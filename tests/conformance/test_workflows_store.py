"""Workflow store contract: revisions, conditional writes and listing order, on both backends.

The revision is what keeps two editors from erasing each other's saves, so every precondition is
asserted on both arms: the memory twin checks it in Python, Postgres under a row lock, and a
create race on Postgres lands on the primary key instead (the last test here).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from felix.workflows import store as workflows
from felix.workflows.schema import WorkflowSpec
from felix.workflows.store import WorkflowConflict

BACKENDS = ["memory", "postgres"]
parametrized = pytest.mark.parametrize("store_settings", BACKENDS, indirect=True)
TENANT = "conformance"


def _spec(name: str = "Triage", **extra: Any) -> WorkflowSpec:
    return WorkflowSpec.model_validate(
        {"name": name, "steps": [{"id": "route", "kind": "branch", "position": {"x": 1.5, "y": 2}}], **extra}
    )


async def _put(settings: Any, workflow_id: str, name: str = "Triage", **kw: Any) -> dict[str, Any]:
    return await workflows.put_workflow(settings, TENANT, workflow_id, _spec(name), **kw)


@parametrized
async def test_a_save_round_trips_and_counts_versions(
    store_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One millisecond for both saves: `updated_at` must still move past the first.
    monkeypatch.setattr(workflows, "now_ms", lambda: 1_800_000_000_000)
    first = await _put(store_settings, "triage", updated_by="op")
    assert first["version"] == 1
    assert first["updated_by"] == "op"
    stored = await workflows.get_workflow(store_settings, TENANT, "triage")
    assert stored is not None
    assert set(stored) == set(first), "a read and a write return the same keys on both arms"
    assert stored["revision"] == first["revision"]
    assert WorkflowSpec.model_validate(stored["definition"]) == _spec()
    # `exclude_none`: an unset position is absent, not a stored null.
    assert "trigger_position" not in stored["definition"]

    second = await _put(store_settings, "triage", "Triage v2", if_revision=first["revision"])
    assert second["version"] == 2
    assert second["revision"] != first["revision"]
    assert second["created_at"] == first["created_at"]
    assert second["updated_at"] > first["updated_at"]
    assert second["updated_by"] == "", "each save names its own author"


@parametrized
async def test_an_unconditional_save_replaces_and_counts(store_settings: Any) -> None:
    await _put(store_settings, "triage")
    replaced = await _put(store_settings, "triage", "Replaced")
    assert replaced["version"] == 2
    assert replaced["definition"]["name"] == "Replaced"


@parametrized
async def test_a_stale_revision_is_a_conflict_carrying_the_current_row(store_settings: Any) -> None:
    first = await _put(store_settings, "triage")
    theirs = await _put(store_settings, "triage", "Theirs", if_revision=first["revision"])

    with pytest.raises(WorkflowConflict) as raised:
        await _put(store_settings, "triage", "Mine", if_revision=first["revision"])
    assert raised.value.current is not None
    assert raised.value.current["revision"] == theirs["revision"]
    assert raised.value.current["definition"]["name"] == "Theirs"
    stored = await workflows.get_workflow(store_settings, TENANT, "triage")
    assert stored is not None and stored["definition"]["name"] == "Theirs", "the losing save wrote nothing"


@parametrized
async def test_a_revision_from_before_a_delete_cannot_save_over_the_recreated_workflow(
    store_settings: Any,
) -> None:
    old = await _put(store_settings, "triage", "Old")
    assert await workflows.delete_workflow(store_settings, TENANT, "triage") is True
    new = await _put(store_settings, "triage", "New")
    assert new["version"] == old["version"] == 1, "both are version 1, which is why version is not enough"
    assert new["revision"] != old["revision"]

    with pytest.raises(WorkflowConflict):
        await _put(store_settings, "triage", "Stale editor", if_revision=old["revision"])
    with pytest.raises(WorkflowConflict):
        await workflows.delete_workflow(store_settings, TENANT, "triage", if_revision=old["revision"])
    stored = await workflows.get_workflow(store_settings, TENANT, "triage")
    assert stored is not None and stored["definition"]["name"] == "New"


@parametrized
async def test_if_absent_creates_only(store_settings: Any) -> None:
    created = await _put(store_settings, "triage", if_absent=True)
    assert created["version"] == 1
    with pytest.raises(WorkflowConflict) as raised:
        await _put(store_settings, "triage", "Again", if_absent=True)
    assert raised.value.current is not None and raised.value.current["revision"] == created["revision"]


@parametrized
async def test_saving_over_a_missing_workflow_is_a_conflict_with_nothing_current(store_settings: Any) -> None:
    with pytest.raises(WorkflowConflict) as raised:
        await _put(store_settings, "gone", if_revision="0123abcd.3")
    assert raised.value.current is None
    assert await workflows.get_workflow(store_settings, TENANT, "gone") is None


@parametrized
async def test_delete_honours_the_revision(store_settings: Any) -> None:
    saved = await _put(store_settings, "triage")
    for stale in ("not-a-revision", saved["revision"] + "0", saved["revision"].replace(".1", ".2")):
        with pytest.raises(WorkflowConflict) as raised:
            await workflows.delete_workflow(store_settings, TENANT, "triage", if_revision=stale)
        assert raised.value.current is not None and raised.value.current["revision"] == saved["revision"]
    assert await workflows.get_workflow(store_settings, TENANT, "triage") is not None
    assert (
        await workflows.delete_workflow(store_settings, TENANT, "triage", if_revision=saved["revision"])
        is True
    )
    assert await workflows.delete_workflow(store_settings, TENANT, "triage") is False
    assert (
        await workflows.delete_workflow(store_settings, TENANT, "triage", if_revision=saved["revision"])
        is False
    )


@parametrized
async def test_the_listing_puts_the_latest_save_first(
    store_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = iter(range(1_800_000_000_000, 1_800_000_001_000))
    monkeypatch.setattr(workflows, "now_ms", lambda: next(clock))
    for workflow_id in ("alpha", "bravo", "alpha"):
        await _put(store_settings, workflow_id, workflow_id)
    assert [w["id"] for w in await workflows.list_workflows(store_settings, TENANT)] == ["alpha", "bravo"]


@parametrized
async def test_workflows_saved_in_one_millisecond_page_the_same_on_both_arms(
    store_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workflows, "now_ms", lambda: 1_800_000_000_000)
    ids = ["alpha", "bravo", "charlie", "delta"]
    for workflow_id in ids:
        await _put(store_settings, workflow_id, workflow_id)
    await workflows.put_workflow(store_settings, "other-tenant", "zulu", _spec())

    full = [w["id"] for w in await workflows.list_workflows(store_settings, TENANT)]
    assert full == sorted(ids, reverse=True)
    assert [w["id"] for w in await workflows.list_workflows(store_settings, TENANT, limit=2)] == full[:2]
    # Out-of-range limits clamp on both arms rather than erroring on one.
    assert len(await workflows.list_workflows(store_settings, TENANT, limit=-1)) == 1
    assert len(await workflows.list_workflows(store_settings, TENANT, limit=0)) == 1


@parametrized
async def test_one_tenant_cannot_read_or_delete_anothers_workflow(store_settings: Any) -> None:
    await workflows.put_workflow(store_settings, "tenant-a", "triage", _spec("A's"))
    await workflows.put_workflow(store_settings, "tenant-b", "triage", _spec("B's"))
    assert await workflows.delete_workflow(store_settings, "tenant-b", "triage") is True
    kept = await workflows.get_workflow(store_settings, "tenant-a", "triage")
    assert kept is not None and kept["definition"]["name"] == "A's"
    assert await workflows.get_workflow(store_settings, "tenant-b", "triage") is None


@parametrized
async def test_a_read_cannot_edit_the_store(store_settings: Any) -> None:
    await _put(store_settings, "triage")
    read = await workflows.get_workflow(store_settings, TENANT, "triage")
    assert read is not None
    read["definition"]["name"] = "edited in place"
    again = await workflows.get_workflow(store_settings, TENANT, "triage")
    assert again is not None and again["definition"]["name"] == "Triage"


@parametrized
async def test_bad_ids_and_contradictory_preconditions_are_refused(store_settings: Any) -> None:
    with pytest.raises(ValueError, match="workflow id"):
        await _put(store_settings, "../etc")
    with pytest.raises(ValueError, match="both"):
        await _put(store_settings, "triage", if_absent=True, if_revision="x.1")


@parametrized
async def test_two_creates_of_one_id_leave_exactly_one_winner(store_settings: Any) -> None:
    # On Postgres neither create finds a row to lock, so both pass the precondition and the loser
    # lands on the primary key; that must surface as a conflict, not an `IntegrityError`.
    results = await asyncio.gather(
        _put(store_settings, "race", "One", if_absent=True),
        _put(store_settings, "race", "Two", if_absent=True),
        return_exceptions=True,
    )
    conflicts = [r for r in results if isinstance(r, WorkflowConflict)]
    saved = [r for r in results if isinstance(r, dict)]
    assert len(conflicts) == 1 and len(saved) == 1, results
    assert conflicts[0].current is not None
    assert conflicts[0].current["revision"] == saved[0]["revision"]
