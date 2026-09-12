"""One contract for the jobs store, run against both backends.

Scheduled jobs are the worker's instructions: what to run, for which tenant, on what
schedule, and whether the last attempt worked. Their Postgres half ran only under
`test_migrations.py`, which creates the schema and never queries it — so everything asserted
about them was asserted about two dicts keyed by tuple.

The semantics here are the ones a dict lookup and a `SELECT` are easy to differ on, and the
ones the scheduler actually depends on: that re-publishing a job keeps its run history rather
than resetting it, that deleting a job takes its runs with it, and that "the most recent runs"
means the same thing on both when several runs share a timestamp — which they do, because a
sweep records a burst of them.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.jobs import store as jobs

BACKENDS = ["memory", "postgres"]
parametrized = pytest.mark.parametrize("store_settings", BACKENDS, indirect=True)

TENANT = "conformance"
OTHER = "other-tenant"
JOB = "nightly-digest"


async def _put(settings: Any, name: str = JOB, **kw: Any) -> dict[str, Any]:
    return await jobs.put_job(
        settings,
        kw.pop("tenant_id", TENANT),
        name,
        schedule=kw.pop("schedule", "0 3 * * *"),
        manifest_id=kw.pop("manifest_id", "quick"),
        payload=kw.pop("payload", {"prompt": "summarise"}),
        **kw,
    )


# --- the job row ----------------------------------------------------------------------------


@parametrized
@pytest.mark.asyncio
async def test_a_job_round_trips_with_every_field(store_settings: Any) -> None:
    created = await _put(store_settings, payload={"prompt": "summarise", "depth": 2})

    fetched = await jobs.get_job(store_settings, TENANT, JOB)

    assert fetched is not None
    assert fetched == created
    assert fetched["schedule"] == "0 3 * * *"
    assert fetched["manifest_id"] == "quick"
    assert fetched["payload"] == {"prompt": "summarise", "depth": 2}
    assert fetched["enabled"] is True
    assert fetched["created_at"] > 0
    # Never run yet, and the scheduler reads these to decide what is due.
    assert fetched["last_run_at"] is None
    assert fetched["next_run_at"] is None
    assert fetched["last_status"] == ""
    assert fetched["last_error"] == ""


@parametrized
@pytest.mark.asyncio
async def test_republishing_a_job_keeps_its_run_state(store_settings: Any) -> None:
    """`put_job` is an upsert, and the scheduler's state lives on the same row as the spec.

    An operator editing a schedule must not silently reset `last_run_at` — the sweep reads it
    to decide what is due, so a reset either re-runs a job immediately or hides that it is
    overdue. `created_at` has to survive for the same reason it exists.
    """
    first = await _put(store_settings)
    await jobs.touch_run(
        store_settings,
        TENANT,
        JOB,
        last_run_at=1_000,
        next_run_at=2_000,
        last_status="ok",
        last_error="",
    )

    updated = await _put(store_settings, schedule="*/5 * * * *", payload={"prompt": "other"}, enabled=False)

    assert updated["schedule"] == "*/5 * * * *"
    assert updated["payload"] == {"prompt": "other"}
    assert updated["enabled"] is False
    # Carried, not reset.
    assert updated["last_run_at"] == 1_000
    assert updated["next_run_at"] == 2_000
    assert updated["last_status"] == "ok"
    assert updated["created_at"] == first["created_at"]


@parametrized
@pytest.mark.asyncio
async def test_touching_a_job_that_is_gone_is_not_an_error(store_settings: Any) -> None:
    """The sweep records a result after the run, and a job can be deleted in between."""
    await jobs.touch_run(store_settings, TENANT, "never-existed", last_run_at=1, last_status="ok")

    assert await jobs.get_job(store_settings, TENANT, "never-existed") is None


@parametrized
@pytest.mark.asyncio
async def test_one_tenants_jobs_are_invisible_to_another(store_settings: Any) -> None:
    await _put(store_settings, tenant_id=TENANT)
    await _put(store_settings, tenant_id=OTHER, manifest_id="deep")

    mine = await jobs.list_jobs(store_settings, TENANT)
    theirs = await jobs.list_jobs(store_settings, OTHER)

    assert [j["manifest_id"] for j in mine] == ["quick"]
    assert [j["manifest_id"] for j in theirs] == ["deep"]
    assert await jobs.get_job(store_settings, OTHER, JOB) is not None
    assert sorted(await jobs.list_tenants_with_jobs(store_settings)) == sorted([TENANT, OTHER])


# --- deletion -------------------------------------------------------------------------------


@parametrized
@pytest.mark.asyncio
async def test_deleting_a_job_takes_its_runs_with_it(store_settings: Any) -> None:
    """Otherwise a job republished under the same name inherits a stranger's history.

    Job names are the primary key, so the name is reusable by design; orphaned runs would
    reappear under the new job and be read as its own.
    """
    await _put(store_settings)
    await jobs.record_run(store_settings, TENANT, JOB, status="ok", started_at=10)

    assert await jobs.delete_job(store_settings, TENANT, JOB) is True

    assert await jobs.get_job(store_settings, TENANT, JOB) is None
    assert await jobs.list_runs(store_settings, TENANT, JOB) == []


@parametrized
@pytest.mark.asyncio
async def test_deleting_a_job_that_is_not_there_reports_it(store_settings: Any) -> None:
    """The route turns this into a 404, so "did anything happen" has to be the truth."""
    assert await jobs.delete_job(store_settings, TENANT, "never-existed") is False


@parametrized
@pytest.mark.asyncio
async def test_deleting_one_tenants_job_leaves_anothers(store_settings: Any) -> None:
    await _put(store_settings, tenant_id=TENANT)
    await _put(store_settings, tenant_id=OTHER)
    await jobs.record_run(store_settings, OTHER, JOB, started_at=10)

    assert await jobs.delete_job(store_settings, TENANT, JOB) is True

    assert await jobs.get_job(store_settings, OTHER, JOB) is not None
    assert len(await jobs.list_runs(store_settings, OTHER, JOB)) == 1


# --- run history ----------------------------------------------------------------------------


@parametrized
@pytest.mark.asyncio
async def test_a_run_round_trips_with_every_field(store_settings: Any) -> None:
    await _put(store_settings)

    recorded = await jobs.record_run(
        store_settings,
        TENANT,
        JOB,
        status="error",
        error="upstream timed out",
        result={"attempted": 3, "nested": {"k": "v"}},
        started_at=100,
        finished_at=250,
    )

    runs = await jobs.list_runs(store_settings, TENANT, JOB)
    assert [r for r in runs] == [recorded]
    row = runs[0]
    assert row["status"] == "error"
    assert row["error"] == "upstream timed out"
    assert row["result"] == {"attempted": 3, "nested": {"k": "v"}}
    assert (row["started_at"], row["finished_at"]) == (100, 250)
    assert row["run_id"]


@parametrized
@pytest.mark.asyncio
async def test_runs_come_back_newest_first(store_settings: Any) -> None:
    await _put(store_settings)
    for started in (10, 30, 20):
        await jobs.record_run(store_settings, TENANT, JOB, started_at=started)

    runs = await jobs.list_runs(store_settings, TENANT, JOB)

    assert [r["started_at"] for r in runs] == [30, 20, 10]


@parametrized
@pytest.mark.asyncio
async def test_the_limit_keeps_the_newest_runs(store_settings: Any) -> None:
    """A truncated history that drops the *newest* rows is worse than no history."""
    await _put(store_settings)
    for started in range(1, 6):
        await jobs.record_run(store_settings, TENANT, JOB, started_at=started * 10)

    runs = await jobs.list_runs(store_settings, TENANT, JOB, limit=2)

    assert [r["started_at"] for r in runs] == [50, 40]


@parametrized
@pytest.mark.asyncio
async def test_runs_sharing_a_timestamp_truncate_the_same_way(store_settings: Any) -> None:
    """`started_at` is milliseconds and a sweep records a burst, so ties are ordinary.

    With no tiebreak, "the most recent two of five" is whichever two the backend happens to
    return — so the twin and the store can disagree about a job's recent history while both
    look healthy, and two identical requests to the same backend need not agree either.
    """
    await _put(store_settings)
    recorded = [
        await jobs.record_run(store_settings, TENANT, JOB, started_at=100, error=f"run-{i}") for i in range(5)
    ]

    page = await jobs.list_runs(store_settings, TENANT, JOB, limit=2)
    again = await jobs.list_runs(store_settings, TENANT, JOB, limit=2)

    assert len(page) == 2
    assert [r["run_id"] for r in page] == [r["run_id"] for r in again], "two identical reads disagreed"
    # `run_id` breaks the tie on both arms, so which two come back is a fact rather than a
    # coincidence of insertion order or query plan. It is arbitrary with respect to *when* the
    # runs happened — there is no finer recency signal than `started_at` to recover — but it is
    # the same arbitrary answer everywhere, which is what the twin standing in for the store
    # requires. Before this, the stable sort on the twin returned the two *oldest*.
    expected = sorted((r["run_id"] for r in recorded), reverse=True)[:2]
    assert [r["run_id"] for r in page] == expected, page


@parametrized
@pytest.mark.asyncio
async def test_one_jobs_runs_are_not_anothers(store_settings: Any) -> None:
    await _put(store_settings, name="a")
    await _put(store_settings, name="b")
    await jobs.record_run(store_settings, TENANT, "a", started_at=10, error="from-a")
    await jobs.record_run(store_settings, TENANT, "b", started_at=20, error="from-b")

    assert [r["error"] for r in await jobs.list_runs(store_settings, TENANT, "a")] == ["from-a"]
    assert [r["error"] for r in await jobs.list_runs(store_settings, TENANT, "b")] == ["from-b"]


@parametrized
@pytest.mark.asyncio
async def test_jobs_are_listed_in_a_stable_order(store_settings: Any) -> None:
    """`GET /jobs` is an operator's inventory, and neither arm ordered it at all.

    The twin returned dict insertion order and Postgres whatever the plan produced, so two
    consecutive calls could disagree and the twin could not stand in for the store.
    """
    for name in ("zeta", "alpha", "mu"):
        await _put(store_settings, name=name)

    listed = await jobs.list_jobs(store_settings, TENANT)

    assert [j["name"] for j in listed] == ["alpha", "mu", "zeta"]
    assert [j["name"] for j in await jobs.list_jobs(store_settings, TENANT)] == ["alpha", "mu", "zeta"]
