"""Workflow run store contract: the compare-and-set the engine relies on, and listing order.

The orchestrator saves a run only over the version it read, so a replayed look cannot overwrite
the one that beat it; both arms must refuse the stale save the same way.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.workflows import runs

BACKENDS = ["memory", "postgres"]
parametrized = pytest.mark.parametrize("store_settings", BACKENDS, indirect=True)
TENANT = "conformance"


def _new(workflow_id: str = "triage", **kw: Any) -> runs.NewRun:
    return runs.NewRun(
        workflow_id=workflow_id,
        revision="g.1",
        definition={"name": "Triage"},
        inputs={"issue": "x"},
        steps={"classify": {"status": "pending"}},
        auth={"principal_sub": "alice", "scopes": []},
        pins={"quick": {"pin": {}, "ttl_s": 300}},
        expires_at=1_900_000_000_000,
        trigger="manual",
        started_by="alice",
        **kw,
    )


@parametrized
async def test_a_run_round_trips_and_its_view_hides_the_caller(store_settings: Any) -> None:
    created = await runs.create_run(store_settings, TENANT, _new())
    stored = await runs.get_run(store_settings, TENANT, created["id"])
    assert stored == created
    assert stored is not None and stored["auth"]["principal_sub"] == "alice"
    view = runs.workflow_run_view(stored)
    assert not {"auth", "pins", "definition", "tenant_id"} & set(view)
    assert view["steps"] == {"classify": {"status": "pending"}}


@parametrized
async def test_a_runs_untrusted_inputs_round_trip_and_default_to_empty(store_settings: Any) -> None:
    plain = await runs.create_run(store_settings, TENANT, _new())
    tainted = await runs.create_run(store_settings, TENANT, _new(untrusted_inputs=("issue",)))
    for created, expected in ((plain, []), (tainted, ["issue"])):
        stored = await runs.get_run(store_settings, TENANT, created["id"])
        assert stored is not None and stored["untrusted_inputs"] == expected
        assert runs.workflow_run_view(stored)["untrusted_inputs"] == expected
    listed = await runs.list_runs(store_settings, TENANT, "triage")
    assert sorted(r["untrusted_inputs"] for r in listed) == [[], ["issue"]]


@parametrized
async def test_ending_a_run_clears_its_recorded_starter_and_pins(store_settings: Any) -> None:
    run = await runs.create_run(store_settings, TENANT, _new())
    run["status"] = "running"
    assert await runs.save_run(store_settings, run)
    running = await runs.get_run(store_settings, TENANT, run["id"])
    assert running is not None and running["auth"]["principal_sub"] == "alice", "kept while it runs"
    assert running["pins"]

    running["status"] = "completed"
    assert await runs.save_run(store_settings, running)
    ended = await runs.get_run(store_settings, TENANT, run["id"])
    assert ended is not None and (ended["auth"], ended["pins"]) == ({}, {})
    assert ended["inputs"] == {"issue": "x"}, "the record itself stays"


@parametrized
async def test_a_save_over_a_stale_version_writes_nothing(store_settings: Any) -> None:
    created = await runs.create_run(store_settings, TENANT, _new())
    first = await runs.get_run(store_settings, TENANT, created["id"])
    second = await runs.get_run(store_settings, TENANT, created["id"])
    assert first is not None and second is not None

    first["steps"]["classify"] = {"status": "running"}
    first["status"] = "running"
    assert await runs.save_run(store_settings, first) is True
    assert first["version"] == 2

    second["status"] = "cancelling"
    assert await runs.save_run(store_settings, second) is False, "it read version 1, which moved"
    stored = await runs.get_run(store_settings, TENANT, created["id"])
    assert stored is not None
    assert (stored["status"], stored["steps"]["classify"]["status"], stored["version"]) == (
        "running",
        "running",
        2,
    )


@parametrized
async def test_reaching_a_terminal_status_stamps_finished_at_once(store_settings: Any) -> None:
    run = await runs.create_run(store_settings, TENANT, _new())
    run["status"] = "completed"
    assert await runs.save_run(store_settings, run)
    stamped = run["finished_at"]
    assert stamped is not None
    assert await runs.save_run(store_settings, run)
    stored = await runs.get_run(store_settings, TENANT, run["id"])
    assert stored is not None and stored["finished_at"] == stamped


@parametrized
async def test_runs_list_newest_first_per_workflow_and_tenant(
    store_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runs, "now_ms", lambda: 1_800_000_000_000)
    ids = ["a" * 32, "b" * 32, "c" * 32]
    for run_id in ids:
        await runs.create_run(store_settings, TENANT, _new(run_id=run_id))
    await runs.create_run(store_settings, TENANT, _new("other", run_id="d" * 32))
    await runs.create_run(store_settings, "other-tenant", _new(run_id="e" * 32))
    listed = [r["id"] for r in await runs.list_runs(store_settings, TENANT, "triage")]
    assert listed == sorted(ids, reverse=True), "a tie on created_at ends on the id, on both arms"
    assert [r["id"] for r in await runs.list_runs(store_settings, TENANT, "triage", limit=1)] == listed[:1]


@parametrized
async def test_a_save_changes_only_what_a_run_may_change(store_settings: Any) -> None:
    # The memory twin once stored the whole row; Postgres writes only the mutable columns, so an
    # edit to the definition, the inputs or the recorded caller must be dropped on both arms.
    run = await runs.create_run(store_settings, TENANT, _new())
    run["definition"] = {"name": "Swapped"}
    run["inputs"] = {"issue": "swapped"}
    run["auth"] = {"principal_sub": "mallory", "scopes": ["admin"]}
    run["status"] = "running"
    assert await runs.save_run(store_settings, run)
    stored = await runs.get_run(store_settings, TENANT, run["id"])
    assert stored is not None
    assert stored["status"] == "running"
    assert stored["definition"] == {"name": "Triage"}
    assert stored["inputs"] == {"issue": "x"}
    assert stored["auth"]["principal_sub"] == "alice"


@parametrized
async def test_active_runs_are_counted_per_tenant(store_settings: Any) -> None:
    done = await runs.create_run(store_settings, TENANT, _new())
    done["status"] = "completed"
    await runs.save_run(store_settings, done)
    await runs.create_run(store_settings, TENANT, _new())
    await runs.create_run(store_settings, "other-tenant", _new())
    assert await runs.count_active(store_settings, TENANT) == 1


@parametrized
async def test_a_save_wakes_whoever_watches_the_run_and_a_refused_one_does_not(
    store_settings: Any,
) -> None:
    from felix.session.notify import thread_watch
    from felix.thread_ids import workflow_run_key

    created = await runs.create_run(store_settings, TENANT, _new())
    first = await runs.get_run(store_settings, TENANT, created["id"])
    stale = await runs.get_run(store_settings, TENANT, created["id"])
    assert first is not None and stale is not None
    key = workflow_run_key(TENANT, created["id"])
    assert key is not None
    async with thread_watch(TENANT, key) as watch:
        first["status"] = "running"
        assert await runs.save_run(store_settings, first)
        assert (await watch.wait(timeout=1.0)).woken, "a save announces the run on its key"
        stale["status"] = "cancelling"
        assert not await runs.save_run(store_settings, stale)
        assert not (await watch.wait(timeout=0.05)).woken, "a save that wrote nothing announces nothing"
