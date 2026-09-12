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

    # A read is a copy, not a window onto the store. The twin was handing back the dict it
    # held, so a caller editing the payload it read edited the stored job; Postgres
    # deserializes a fresh dict per read, which is the archetypal dict-versus-SELECT
    # divergence this contract exists to find.
    fetched["payload"]["prompt"] = "tampered"
    again = await jobs.get_job(store_settings, TENANT, JOB)
    assert again is not None
    assert again["payload"]["prompt"] == "summarise", again

    # And the write side, which aliased in the same way the read side did: the twin kept the
    # caller's dict, so editing what you passed in edited the stored job, while Postgres had
    # serialized it at commit and was unaffected.
    handed_in = {"prompt": "handed in"}
    await _put(store_settings, name="written", payload=handed_in)
    handed_in["prompt"] = "tampered after the write"
    written = await jobs.get_job(store_settings, TENANT, "written")
    assert written is not None
    assert written["payload"]["prompt"] == "handed in", written


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
        last_error="upstream timed out",
    )

    updated = await _put(store_settings, schedule="*/5 * * * *", payload={"prompt": "other"}, enabled=False)

    assert updated["schedule"] == "*/5 * * * *"
    assert updated["payload"] == {"prompt": "other"}
    assert updated["enabled"] is False
    # Carried, not reset.
    assert updated["last_run_at"] == 1_000
    assert updated["next_run_at"] == 2_000
    assert updated["last_status"] == "ok"
    # `last_error` too: the reset value is `""`, so asserting the empty string would have
    # passed whether it was carried or cleared.
    assert updated["last_error"] == "upstream timed out"
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
    # Not `sorted(...)` on the result: both implementations promise sorted output, and this is
    # the third listing in the module whose order this branch is about.
    assert await jobs.list_tenants_with_jobs(store_settings) == sorted([TENANT, OTHER])


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
    """The tenant predicate *on the cascade*, which is a different property from the cascade.

    Only `OTHER` has a run here, so there is no `TENANT` run for a missing cascade to leave
    behind — removing the run delete entirely leaves this green. What it catches is the delete
    losing its tenant filter, which would take another tenant's history with it. The test above
    is the one that proves runs are deleted at all.
    """
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
    assert runs == [recorded]
    row = runs[0]
    assert row["status"] == "error"
    assert row["error"] == "upstream timed out"
    assert row["result"] == {"attempted": 3, "nested": {"k": "v"}}
    assert (row["started_at"], row["finished_at"]) == (100, 250)
    assert row["run_id"]

    # Same for a run's result, and for the same reason.
    row["result"]["attempted"] = 99
    assert (await jobs.list_runs(store_settings, TENANT, JOB))[0]["result"]["attempted"] == 3


@parametrized
@pytest.mark.asyncio
async def test_runs_come_back_newest_first(store_settings: Any) -> None:
    await _put(store_settings)
    for started in (10, 30, 20):
        await jobs.record_run(store_settings, TENANT, JOB, started_at=started)

    runs = await jobs.list_runs(store_settings, TENANT, JOB)

    assert [r["started_at"] for r in runs] == [30, 20, 10]
    # The defaults, which every other call here supplies and so never exercises: the twin
    # reads `status` through `.get("status", "ok")` and Postgres through a server default, and
    # an absent `result` has to arrive as `{}` rather than `None` on both.
    assert [r["status"] for r in runs] == ["ok", "ok", "ok"]
    assert [r["result"] for r in runs] == [{}, {}, {}]


@parametrized
@pytest.mark.asyncio
async def test_the_limit_keeps_the_newest_runs(store_settings: Any) -> None:
    """A truncated history that drops the *newest* rows is worse than no history."""
    await _put(store_settings)
    for started in range(1, 6):
        await jobs.record_run(store_settings, TENANT, JOB, started_at=started * 10)

    runs = await jobs.list_runs(store_settings, TENANT, JOB, limit=2)

    assert [r["started_at"] for r in runs] == [50, 40]
    # The boundary the store clamps. The route rejects a negative limit, but `list_runs` is a
    # public function the worker calls directly, and a negative one used to slice the twin's
    # list from the end while Postgres refused it outright.
    assert await jobs.list_runs(store_settings, TENANT, JOB, limit=0) == []
    assert await jobs.list_runs(store_settings, TENANT, JOB, limit=-1) == []


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
    """By job *and* by tenant, because job names are chosen per tenant and collide freely.

    Nothing in the repo pinned the tenant half: every other test here records runs under one
    tenant at a time, so dropping `tenant_id` from the query left all of them green while
    `GET /jobs/{name}/runs` returned another tenant's history.
    """
    await _put(store_settings, name="a")
    await _put(store_settings, name="b")
    await _put(store_settings, tenant_id=OTHER, name="a")
    await jobs.record_run(store_settings, TENANT, "a", started_at=10, error="from-a")
    await jobs.record_run(store_settings, TENANT, "b", started_at=20, error="from-b")
    await jobs.record_run(store_settings, OTHER, "a", started_at=30, error="from-other-tenant")

    assert [r["error"] for r in await jobs.list_runs(store_settings, TENANT, "a")] == ["from-a"]
    assert [r["error"] for r in await jobs.list_runs(store_settings, TENANT, "b")] == ["from-b"]
    assert [r["error"] for r in await jobs.list_runs(store_settings, OTHER, "a")] == ["from-other-tenant"]


@parametrized
@pytest.mark.asyncio
async def test_jobs_are_listed_in_a_stable_order(store_settings: Any) -> None:
    """`GET /jobs` is an operator's inventory, and neither arm ordered it at all.

    The twin returned dict insertion order and Postgres whatever the plan produced, so two
    consecutive calls could disagree and the twin could not stand in for the store.

    Both arms pin it, now that the corpus is mixed case. It did not always: with three
    lowercase names the Postgres arm passed for the wrong reason, because `jobs_pkey` is
    `(tenant_id, name)` and an index-only scan hands back name order for free. `jobs_pkey` is
    in the database's *default* collation, so on CI's en_US.utf8 an index-order scan now
    yields roughly `alpha, m-1, m1, _mu, Zeta, zeta` against a code-point expectation — red if
    the `ORDER BY` is reverted, and red if the `COLLATE "C"` is dropped.
    """
    # Mixed case and punctuation on purpose. All-lowercase names sort identically under
    # Python and under every Postgres collation, so a corpus of them shows each arm is
    # self-consistent and nothing about the arms agreeing. CI's image inherits en_US.utf8,
    # which sorts `alpha` before `Zeta` and ignores punctuation at the primary level, where
    # Python puts every capital first — so this is what the `COLLATE "C"` in the store is for,
    # and what goes red without it. Job names are unvalidated URL path segments.
    for name in ("zeta", "Zeta", "alpha", "_mu", "m-1", "m1"):
        await _put(store_settings, name=name)

    expected = sorted(("zeta", "Zeta", "alpha", "_mu", "m-1", "m1"))
    listed = await jobs.list_jobs(store_settings, TENANT)

    assert [j["name"] for j in listed] == expected
    assert [j["name"] for j in await jobs.list_jobs(store_settings, TENANT)] == expected
