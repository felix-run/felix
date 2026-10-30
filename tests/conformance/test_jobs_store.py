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
from felix.jobs.schedule import ScheduleError

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
async def test_a_job_records_its_writer_and_the_next_write_replaces_them(store_settings: Any) -> None:
    """Who a job runs for (`felix.jobs.scheduler`): the last writer, with exactly their authority."""
    alice = {"scopes": ["jobs:write", "x:read"], "scheme": "api_key", "anonymous": False}
    created = await _put(store_settings, created_by="alice", created_auth=alice)
    assert (created["created_by"], created["created_auth"]) == ("alice", alice)
    fetched = await jobs.get_job(store_settings, TENANT, JOB)
    assert (fetched["created_by"], fetched["created_auth"]) == ("alice", alice)

    bob = {"scopes": ["jobs:write"], "scheme": "access", "anonymous": False, "felix_manifests": ["quick"]}
    await _put(store_settings, created_by="bob", created_auth=bob)
    fetched = await jobs.get_job(store_settings, TENANT, JOB)
    assert (fetched["created_by"], fetched["created_auth"]) == ("bob", bob)
    (listed,) = await jobs.list_jobs(store_settings, TENANT)
    assert (listed["created_by"], listed["created_auth"]) == ("bob", bob)

    # Written with no writer -- what every row before `0042_job_creator` holds -- reads as none.
    await _put(store_settings)
    fetched = await jobs.get_job(store_settings, TENANT, JOB)
    assert (fetched["created_by"], fetched["created_auth"]) == (None, None)


@parametrized
async def test_a_workflow_job_keeps_its_workflow_and_a_rewrite_can_drop_it(store_settings: Any) -> None:
    created = await _put(
        store_settings,
        manifest_id="",
        workflow_id="triage",
        workflow_revision="g1.3",
        payload={"inputs": {"n": 1}},
    )
    assert (created["manifest_id"], created["workflow_id"], created["workflow_revision"]) == (
        "",
        "triage",
        "g1.3",
    )
    fetched = await jobs.get_job(store_settings, TENANT, JOB)
    assert fetched == created
    (listed,) = await jobs.list_jobs(store_settings, TENANT)
    assert listed["workflow_id"] == "triage"

    # Rewritten as a manifest job: the workflow goes, rather than both lingering on the row.
    await _put(store_settings)
    fetched = await jobs.get_job(store_settings, TENANT, JOB)
    assert (fetched["manifest_id"], fetched["workflow_id"], fetched["workflow_revision"]) == ("quick", "", "")


@parametrized
async def test_a_job_naming_a_manifest_and_a_workflow_is_refused(store_settings: Any) -> None:
    with pytest.raises(jobs.BothTargets):
        await _put(store_settings, workflow_id="triage")
    assert await jobs.get_job(store_settings, TENANT, JOB) is None


@parametrized
async def test_a_job_round_trips_with_every_field(store_settings: Any) -> None:
    created = await _put(store_settings, payload={"prompt": "summarise", "depth": 2})

    fetched = await jobs.get_job(store_settings, TENANT, JOB)

    assert fetched is not None
    assert fetched == created
    assert fetched["schedule"] == "0 3 * * *"
    assert fetched["manifest_id"] == "quick"
    assert fetched["workflow_id"] == ""
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
    # Nested on purpose: a shallow `dict(payload)` passes a top-level mutation, so only a
    # nested one pins the deepcopy the store actually does. Job payloads nest in practice —
    # this is an arbitrary JSON body off `PUT /jobs/{name}`.
    handed_in = {"prompt": "handed in", "opts": {"depth": 1}}
    await _put(store_settings, name="written", payload=handed_in)
    handed_in["opts"]["depth"] = 99
    written = await jobs.get_job(store_settings, TENANT, "written")
    assert written is not None
    assert written["payload"]["opts"]["depth"] == 1, written

    # The worker-written half, which had the same fix and no test.
    handed_result = {"counts": {"scanned": 2}}
    await jobs.record_run(store_settings, TENANT, "written", started_at=5, result=handed_result)
    handed_result["counts"]["scanned"] = 99
    runs = await jobs.list_runs(store_settings, TENANT, "written")
    assert runs[0]["result"]["counts"]["scanned"] == 2, runs


@parametrized
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
async def test_touching_a_job_that_is_gone_is_not_an_error(store_settings: Any) -> None:
    """The sweep records a result after the run, and a job can be deleted in between."""
    await jobs.touch_run(store_settings, TENANT, "never-existed", last_run_at=1, last_status="ok")

    assert await jobs.get_job(store_settings, TENANT, "never-existed") is None


@parametrized
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
async def test_deleting_a_job_that_is_not_there_reports_it(store_settings: Any) -> None:
    """The route turns this into a 404, so "did anything happen" has to be the truth."""
    assert await jobs.delete_job(store_settings, TENANT, "never-existed") is False


@parametrized
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


# --- trimming the run history (`FELIX_JOB_RUNS_KEEP`) and the daily count -----------------------


def _keeping(settings: Any, keep: int) -> Any:
    return settings.model_copy(update={"job_runs_keep": keep})


async def _started(settings: Any, name: str = JOB, tenant: str = TENANT) -> list[int]:
    return [r["started_at"] for r in await jobs.list_runs(settings, tenant, name, limit=1000)]


@parametrized
async def test_recording_a_run_keeps_only_the_newest_n_of_that_job(store_settings: Any) -> None:
    """One row per firing, forever, until this: each `record_run` trims its own job to the newest
    `job_runs_keep`. Another job's history -- the same tenant's, or another tenant's job of the same
    name -- is written first with trimming off and must come through every trim below untouched."""
    unbounded = _keeping(store_settings, 0)
    for started in range(1, 5):
        await jobs.record_run(unbounded, TENANT, "other-job", started_at=started)
        await jobs.record_run(unbounded, OTHER, JOB, started_at=started)

    keep3 = _keeping(store_settings, 3)
    for started in (10, 50, 30, 20, 40):
        await jobs.record_run(keep3, TENANT, JOB, started_at=started)

    assert await _started(store_settings) == [50, 40, 30], "the newest three, by started_at"
    assert await _started(store_settings, "other-job") == [4, 3, 2, 1], (
        "another job's runs are not this one's"
    )
    assert await _started(store_settings, JOB, OTHER) == [4, 3, 2, 1], "nor another tenant's"


@parametrized
async def test_keep_zero_keeps_every_run(store_settings: Any) -> None:
    unbounded = _keeping(store_settings, 0)
    for started in range(1, 8):
        await jobs.record_run(unbounded, TENANT, JOB, started_at=started)
    assert await _started(store_settings) == [7, 6, 5, 4, 3, 2, 1]
    assert await jobs.trim_runs(store_settings, TENANT, JOB, keep=0, protect_since=10**15) == 0


@parametrized
async def test_runs_sharing_a_timestamp_are_trimmed_the_way_they_are_listed(store_settings: Any) -> None:
    """Which tied run survives is the `run_id` tiebreak `list_runs` pages by, on both arms -- not
    insertion order on one and the plan's order on the other."""
    unbounded = _keeping(store_settings, 0)
    recorded = [await jobs.record_run(unbounded, TENANT, JOB, started_at=100) for _ in range(5)]

    assert await jobs.trim_runs(store_settings, TENANT, JOB, keep=2, protect_since=10**15) == 3

    kept = [r["run_id"] for r in await jobs.list_runs(store_settings, TENANT, JOB)]
    assert kept == sorted((r["run_id"] for r in recorded), reverse=True)[:2]


@parametrized
async def test_todays_runs_that_ran_outlive_the_trim_and_skipped_ones_do_not(
    store_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A heartbeat's daily cap counts today's runs (`count_runs_since`), so a trim that took them
    would hand it firings back; a person asking for skipped runs could push them out. A skipped run
    counts for nothing and is trimmed like any old one."""
    # Pinned mid-day, so the runs below cannot straddle a real UTC midnight.
    now = 1_800_000_000_000 - 1_800_000_000_000 % jobs.DAY_MS + 12 * 3_600_000
    monkeypatch.setattr(jobs, "now_ms", lambda: now)
    today = jobs.utc_day_start(now)
    keep2 = _keeping(store_settings, 2)
    await jobs.record_run(keep2, TENANT, JOB, started_at=today - 1)
    for offset in (3, 2, 1):
        await jobs.record_run(keep2, TENANT, JOB, started_at=now - offset)
    for _ in range(3):
        await jobs.record_run(keep2, TENANT, JOB, started_at=now, status=jobs.SKIPPED)

    runs = await jobs.list_runs(store_settings, TENANT, JOB, limit=100)
    assert [(r["started_at"] - now, r["status"]) for r in runs] == [
        (0, "skipped"),
        (0, "skipped"),
        (-1, "ok"),
        (-2, "ok"),
        (-3, "ok"),
    ], "yesterday's run and the oldest skipped one went; today's three that ran stayed"
    assert await jobs.count_runs_since(store_settings, TENANT, JOB, today) == 3


@parametrized
async def test_a_trim_takes_at_most_a_batch_and_the_oldest_first(
    store_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A history from before trimming existed shrinks over several runs, never in one statement --
    and each batch is the *oldest* rows past the keep, so the newest are never what a batch takes."""
    for started in range(1, 7):
        await jobs.record_run(_keeping(store_settings, 0), TENANT, JOB, started_at=started)
    monkeypatch.setattr(jobs, "TRIM_BATCH", 2)

    assert await jobs.trim_runs(store_settings, TENANT, JOB, keep=1, protect_since=10**15) == 2
    assert await _started(store_settings) == [6, 5, 4, 3]
    assert await jobs.trim_runs(store_settings, TENANT, JOB, keep=1, protect_since=10**15) == 2
    assert await jobs.trim_runs(store_settings, TENANT, JOB, keep=1, protect_since=10**15) == 1
    assert await _started(store_settings) == [6]


@parametrized
async def test_a_trim_that_fails_keeps_the_run_it_followed(
    store_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _down(*_a: Any, **_kw: Any) -> int:
        raise RuntimeError("store down")

    monkeypatch.setattr(jobs, "trim_runs", _down)
    recorded = await jobs.record_run(_keeping(store_settings, 1), TENANT, JOB, started_at=10)
    assert [r["run_id"] for r in await jobs.list_runs(store_settings, TENANT, JOB)] == [recorded["run_id"]]


@parametrized
async def test_count_runs_since_counts_one_jobs_runs_that_ran(store_settings: Any) -> None:
    unbounded = _keeping(store_settings, 0)
    for started, status in ((99, "ok"), (100, "ok"), (150, "error"), (200, jobs.SKIPPED)):
        await jobs.record_run(unbounded, TENANT, JOB, started_at=started, status=status)
    await jobs.record_run(unbounded, TENANT, "other-job", started_at=120)
    await jobs.record_run(unbounded, OTHER, JOB, started_at=120)

    assert await jobs.count_runs_since(store_settings, TENANT, JOB, 100) == 2, "from 100, never skipped"
    assert await jobs.count_runs_since(store_settings, TENANT, JOB, 201) == 0
    assert await jobs.count_runs_since(store_settings, TENANT, "nobody", 0) == 0


@parametrized
async def test_jobs_are_listed_in_a_stable_order(store_settings: Any) -> None:
    """`GET /jobs` is an operator's inventory, and neither arm ordered it at all.

    The twin returned dict insertion order and Postgres whatever the plan produced, so two
    consecutive calls could disagree and the twin could not stand in for the store.

    The memory arm pins it unconditionally. The Postgres arm pins it *on a cluster whose
    default collation is not `C`* — which CI is, since `pgvector/pgvector:pg17` inherits
    en_US.utf8, and where an index-order scan of `jobs_pkey` now yields roughly
    `alpha, m-1, m1, _mu, Zeta, zeta` against a code-point expectation, red if the `ORDER BY`
    is reverted and red if the `COLLATE "C"` is dropped.

    On a `C`-collated cluster — an alpine image, an explicit `initdb --locale=C` — that arm
    silently returns to passing for the wrong reason, because `jobs_pkey` then hands back the
    expected order for free. The condition is environmental and nothing here checks it, so
    read this arm as evidence only when you know the cluster's `datcollate`.
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


@parametrized
async def test_two_ticks_claiming_one_due_run_fire_it_once(store_settings: Any) -> None:
    """Two scheduler ticks read the same due job -- two workers, or a slow tick overlapping the
    next. `touch_run` let both claim it; `claim_run` lets the first, and refuses the second."""
    import asyncio

    await _put(store_settings, schedule="*/5 * * * *")
    await jobs.touch_run(store_settings, TENANT, JOB, last_run_at=0, next_run_at=1_000)

    claims = await asyncio.gather(
        *(
            jobs.claim_run(
                store_settings, TENANT, JOB, seen_next_run_at=1_000, last_run_at=2_000, next_run_at=301_000
            )
            for _ in range(2)
        )
    )
    assert sorted(claims) == [False, True]
    (row,) = [j for j in await jobs.list_jobs(store_settings, TENANT) if j["name"] == JOB]
    assert (row["next_run_at"], row["last_status"]) == (301_000, "running")


@parametrized
async def test_a_running_job_is_not_claimed_until_its_mark_goes_stale(store_settings: Any) -> None:
    """`claim_run` moved `next_run_at` and wrote `running`, and read only the first: a run that
    outlasted its interval was claimed again on a later tick. Both arms read the mark now -- one
    `UPDATE ... WHERE` on Postgres -- and both let a mark older than `RUNNING_STALE_MS` go, the
    boundary itself still holding."""
    await _put(store_settings, schedule="*/5 * * * *")
    await jobs.touch_run(store_settings, TENANT, JOB, last_run_at=0, next_run_at=1_000)
    t0 = 1_800_000_000_000
    assert await jobs.claim_run(
        store_settings, TENANT, JOB, seen_next_run_at=1_000, last_run_at=t0, next_run_at=2_000
    )
    stale = t0 + jobs.RUNNING_STALE_MS
    for at in (t0 + 300_000, stale):
        assert not await jobs.claim_run(
            store_settings, TENANT, JOB, seen_next_run_at=2_000, last_run_at=at, next_run_at=3_000
        ), f"claimed a job still running at {at}"
        assert not await jobs.claim_manual_run(store_settings, TENANT, JOB, last_run_at=at)
    row = await jobs.get_job(store_settings, TENANT, JOB)
    assert (row["next_run_at"], row["last_run_at"], row["last_status"]) == (2_000, t0, "running")

    assert await jobs.claim_run(
        store_settings, TENANT, JOB, seen_next_run_at=2_000, last_run_at=stale + 1, next_run_at=3_000
    )
    # Ended normally, the mark is gone and a manual run may claim it -- leaving the schedule.
    await jobs.touch_run(
        store_settings, TENANT, JOB, last_run_at=stale + 1, next_run_at=jobs.KEEP_SCHEDULE, last_status="ok"
    )
    assert await jobs.claim_manual_run(store_settings, TENANT, JOB, last_run_at=stale + 2)
    row = await jobs.get_job(store_settings, TENANT, JOB)
    assert (row["next_run_at"], row["last_run_at"], row["last_status"]) == (3_000, stale + 2, "running")
    assert not await jobs.claim_manual_run(store_settings, TENANT, "no-such-job", last_run_at=stale)


@parametrized
async def test_a_runs_end_is_written_only_over_its_own_claim(store_settings: Any) -> None:
    """`finish_run` compare-and-sets on `last_run_at`: a run reclaimed as stale cannot overwrite
    the claim that reclaimed it, on either arm."""
    await _put(store_settings, schedule="*/5 * * * *")
    await jobs.touch_run(store_settings, TENANT, JOB, last_run_at=0, next_run_at=1_000)
    assert await jobs.claim_run(
        store_settings, TENANT, JOB, seen_next_run_at=1_000, last_run_at=5_000, next_run_at=2_000
    )
    assert not await jobs.finish_run(store_settings, TENANT, JOB, started_at=4_999, last_status="ok")
    assert (await jobs.get_job(store_settings, TENANT, JOB))["last_status"] == "running"
    assert await jobs.finish_run(
        store_settings, TENANT, JOB, started_at=5_000, last_status="error", last_error="boom"
    )
    row = await jobs.get_job(store_settings, TENANT, JOB)
    assert (row["last_status"], row["last_error"], row["next_run_at"]) == ("error", "boom", 2_000)
    assert not await jobs.finish_run(
        store_settings, TENANT, "no-such-job", started_at=5_000, last_status="ok"
    )


@parametrized
async def test_disabling_a_running_job_clears_its_mark(store_settings: Any) -> None:
    await _put(store_settings, schedule="*/5 * * * *")
    await jobs.touch_run(store_settings, TENANT, JOB, last_run_at=0, next_run_at=1_000)
    assert await jobs.claim_run(
        store_settings, TENANT, JOB, seen_next_run_at=1_000, last_run_at=5_000, next_run_at=2_000
    )
    await _put(store_settings, schedule="*/5 * * * *", enabled=False)
    row = await jobs.get_job(store_settings, TENANT, JOB)
    assert (row["last_status"], row["last_error"]) == ("error", jobs.DISABLED_WHILE_RUNNING)
    assert row["last_run_at"] == 5_000 and row["next_run_at"] == 2_000


@parametrized
async def test_a_job_with_no_due_time_is_claimed_once_too(store_settings: Any) -> None:
    """A scheduled job that has never run has a NULL `next_run_at`, which the scheduler treats as
    due, and it must still be claimable -- on both arms. The second claim loses because the first
    moved the due time, not because of how NULL compares."""
    await _put(store_settings, schedule="*/5 * * * *")
    (row,) = [j for j in await jobs.list_jobs(store_settings, TENANT) if j["name"] == JOB]
    assert row["next_run_at"] is None, "this test is about the NULL case"
    first = await jobs.claim_run(
        store_settings, TENANT, JOB, seen_next_run_at=None, last_run_at=1, next_run_at=9
    )
    second = await jobs.claim_run(
        store_settings, TENANT, JOB, seen_next_run_at=None, last_run_at=1, next_run_at=9
    )
    assert (first, second) == (True, False)


@parametrized
async def test_a_one_shot_is_due_at_its_moment_and_stays_fired(store_settings: Any) -> None:
    """`at:` sets the due time on the write that creates it -- on both arms, since Postgres builds
    a new row by a different branch than the twin -- and a rewrite after its claim keeps it fired."""
    at = "at:2999-01-01T00:00:00Z"
    created = await _put(store_settings, schedule=at)
    assert created["next_run_at"] == 32_472_144_000_000
    (row,) = await jobs.list_jobs(store_settings, TENANT)
    assert row["next_run_at"] == 32_472_144_000_000

    claimed = await jobs.claim_run(
        store_settings, TENANT, JOB, seen_next_run_at=32_472_144_000_000, last_run_at=1, next_run_at=None
    )
    assert claimed
    rewritten = await _put(store_settings, schedule=at, payload={"prompt": "edited"})
    assert rewritten["next_run_at"] is None
    assert (await jobs.get_job(store_settings, TENANT, JOB))["next_run_at"] is None

    with pytest.raises(ScheduleError, match="in the past"):
        await _put(store_settings, name="late", schedule="at:2001-01-01T00:00:00Z")
    assert await jobs.get_job(store_settings, TENANT, "late") is None


@parametrized
async def test_first_run_at_sets_a_new_recurring_jobs_due_time_and_a_rewrite_ignores_it(
    store_settings: Any,
) -> None:
    """`jobs/agent_tools.py` writes a recurring task due at its schedule's first slot, not on the
    next sweep. Only the write that creates the row may set it: a rewrite keeps the due time held."""
    created = await _put(store_settings, schedule="@daily", first_run_at=5_000)
    assert created["next_run_at"] == 5_000
    assert (await jobs.get_job(store_settings, TENANT, JOB))["next_run_at"] == 5_000

    rewritten = await _put(store_settings, schedule="@daily", first_run_at=9_000)
    assert rewritten["next_run_at"] == 5_000
    assert (await jobs.get_job(store_settings, TENANT, JOB))["next_run_at"] == 5_000


# --- set_schedule: a conditional move, never an upsert ---------------------------------------


@parametrized
async def test_set_schedule_moves_only_the_schedule_it_was_told_it_would_find(store_settings: Any) -> None:
    await _put(store_settings, schedule="every:60m", enabled=False, created_by="alice")
    assert await jobs.set_schedule(
        store_settings,
        TENANT,
        JOB,
        schedule="every:2h",
        next_run_at=123,
        timezone=None,
        expected_schedule="every:60m",
        expected_timezone=None,
    )
    fetched = await jobs.get_job(store_settings, TENANT, JOB)
    assert fetched is not None
    assert (fetched["schedule"], fetched["next_run_at"]) == ("every:2h", 123)
    assert (fetched["enabled"], fetched["created_by"]) == (False, "alice"), "nothing else moves"

    # Moved since it was read: left alone.
    assert not await jobs.set_schedule(
        store_settings,
        TENANT,
        JOB,
        schedule="every:3h",
        next_run_at=456,
        timezone=None,
        expected_schedule="every:60m",
        expected_timezone=None,
    )
    fetched = await jobs.get_job(store_settings, TENANT, JOB)
    assert fetched is not None and fetched["schedule"] == "every:2h"


@parametrized
async def test_set_schedule_never_brings_back_a_deleted_job(store_settings: Any) -> None:
    await _put(store_settings, schedule="every:60m")
    assert await jobs.delete_job(store_settings, TENANT, JOB)
    assert not await jobs.set_schedule(
        store_settings,
        TENANT,
        JOB,
        schedule="every:2h",
        next_run_at=1,
        timezone=None,
        expected_schedule="every:60m",
        expected_timezone=None,
    )
    assert await jobs.get_job(store_settings, TENANT, JOB) is None


@parametrized
async def test_set_schedule_compares_and_moves_the_zone_too(store_settings: Any) -> None:
    """A heartbeat's zone moves with the manifest's (`heartbeat.keep_schedule_current`), only over
    the zone the firing read -- a NULL one included, which SQL compares only with `IS [NOT]
    DISTINCT FROM`, never `=`."""
    await _put(store_settings, schedule="0 8 * * *")
    assert not await jobs.set_schedule(
        store_settings,
        TENANT,
        JOB,
        schedule="0 9 * * *",
        timezone="Europe/London",
        next_run_at=1,
        expected_schedule="0 8 * * *",
        expected_timezone="America/New_York",
    ), "the zone it held is not the one expected"
    assert await jobs.set_schedule(
        store_settings,
        TENANT,
        JOB,
        schedule="0 9 * * *",
        timezone="Europe/London",
        next_run_at=2,
        expected_schedule="0 8 * * *",
        expected_timezone=None,
    )
    fetched = await jobs.get_job(store_settings, TENANT, JOB)
    assert (fetched["schedule"], fetched["timezone"], fetched["next_run_at"]) == (
        "0 9 * * *",
        "Europe/London",
        2,
    )
    assert await jobs.set_schedule(
        store_settings,
        TENANT,
        JOB,
        schedule="every:2h",
        timezone=None,
        next_run_at=3,
        expected_schedule="0 9 * * *",
        expected_timezone="Europe/London",
    )
    assert (await jobs.get_job(store_settings, TENANT, JOB))["timezone"] is None


# --- timezone (`0051_job_timezone`) -------------------------------------------------------------


@parametrized
async def test_a_jobs_zone_round_trips_and_a_rewrite_without_one_is_utc(store_settings: Any) -> None:
    created = await _put(store_settings, schedule="0 8 * * 1-5", timezone="America/New_York")
    assert created["timezone"] == "America/New_York"
    fetched = await jobs.get_job(store_settings, TENANT, JOB)
    assert fetched == created
    (listed,) = await jobs.list_jobs(store_settings, TENANT)
    assert listed["timezone"] == "America/New_York"

    # Replaced like every other field: left out, the job is back on UTC rather than keeping a zone.
    await _put(store_settings, schedule="0 8 * * 1-5")
    assert (await jobs.get_job(store_settings, TENANT, JOB))["timezone"] is None


@parametrized
@pytest.mark.parametrize(
    ("schedule", "zone"),
    [("every:30m", "Europe/London"), ("", "Europe/London"), ("0 8 * * *", "Mars/Olympus_Mons")],
)
async def test_a_zone_that_cannot_apply_is_refused_and_nothing_is_written(
    store_settings: Any, schedule: str, zone: str
) -> None:
    with pytest.raises(ScheduleError, match=r"^timezone: "):
        await _put(store_settings, schedule=schedule, timezone=zone)
    assert await jobs.get_job(store_settings, TENANT, JOB) is None


@parametrized
async def test_a_new_zone_is_a_new_schedule_for_the_due_time_it_keeps(
    store_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rewrite keeps the due time held while the schedule is unchanged; a changed zone is a
    changed schedule, so the job is due at the earlier of the two."""
    monkeypatch.setattr(jobs, "now_ms", lambda: 1_793_340_000_000)  # 2026-10-30T06:00Z, 15:00 Tokyo
    await _put(
        store_settings, schedule="0 8 * * *", timezone="America/New_York", first_run_at=9_999_999_999_999
    )
    kept = await _put(store_settings, schedule="0 8 * * *", timezone="America/New_York")
    assert kept["next_run_at"] == 9_999_999_999_999
    moved = await _put(store_settings, schedule="0 8 * * *", timezone="Asia/Tokyo")
    assert moved["next_run_at"] == 1_793_401_200_000, "2026-10-30T23:00Z: Tokyo's next 08:00"
    assert (await jobs.get_job(store_settings, TENANT, JOB))["next_run_at"] == moved["next_run_at"]


# --- health incidents (`felix.jobs.watch`) ------------------------------------------------------


@parametrized
async def test_health_is_written_only_over_what_was_seen_and_survives_a_resave(store_settings: Any) -> None:
    """The watch's compare-and-set: the JSONB compared whole on Postgres, the dict on the twin. A
    sweep holding a stale read must lose, or two overlapping sweeps each alert the same incident;
    and `put_job` must leave it, or re-saving a job re-alerts."""
    created = await _put(store_settings)
    assert created["health"] == {}
    opened = {
        "failing": {"since": 5, "opened_at": 9, "error_class": "run_error", "alert": "pending", "attempts": 0}
    }

    assert await jobs.set_health(store_settings, TENANT, JOB, seen={}, health=opened)
    assert not await jobs.set_health(store_settings, TENANT, JOB, seen={}, health={"overdue": {"since": 1}})
    posted = {"failing": {**opened["failing"], "alert": "posted"}}
    assert await jobs.set_health(store_settings, TENANT, JOB, seen=opened, health=posted)

    resaved = await _put(store_settings, schedule="*/5 * * * *")
    assert resaved["health"] == posted
    fetched = await jobs.get_job(store_settings, TENANT, JOB)
    assert fetched is not None and fetched["health"] == posted
    assert not await jobs.set_health(store_settings, TENANT, "gone", seen={}, health=opened)
    assert not await jobs.set_health(store_settings, OTHER, JOB, seen=posted, health={})


# --- reader's preferences (`0052_job_preferences`) --------------------------------------------


@parametrized
async def test_preferences_survive_their_writers_rewrites_and_no_one_elses(store_settings: Any) -> None:
    """`set_preferences` writes them alone. `put_job` by the job's own writer carries them over --
    run state, incidents and all; one by another writer clears them, since the text would otherwise
    run on that writer's scopes. `preferences_required` is the writer's, replaced on every write."""
    created = await _put(store_settings, created_by="carol", preferences_required=True)
    assert (created["preferences"], created["preferences_updated_by"], created["preferences_required"]) == (
        None,
        None,
        True,
    )
    text = "Cover open incidents only.\nSkip <b>staffing</b> -- one paragraph, plain words. é"
    written = await jobs.set_preferences(store_settings, TENANT, JOB, text, by="carol", writer="carol")
    assert written is not None
    assert (written["preferences"], written["preferences_updated_by"]) == (text, "carol")
    stamped = written["preferences_updated_at"]
    assert isinstance(stamped, int) and stamped > 0
    assert await jobs.set_health(store_settings, TENANT, JOB, seen={}, health={"overdue": {"since": 1}})

    resaved = await _put(
        store_settings, schedule="*/5 * * * *", created_by="carol", preferences_required=True
    )
    kept = await jobs.get_job(store_settings, TENANT, JOB)
    (listed,) = await jobs.list_jobs(store_settings, TENANT)
    for row in (resaved, kept, listed):
        assert row is not None
        assert (row["preferences"], row["preferences_updated_at"], row["preferences_updated_by"]) == (
            text,
            stamped,
            "carol",
        )
        assert (row["preferences_required"], row["health"]) == (True, {"overdue": {"since": 1}})

    # Leaving it out of a write is false: replaced like the rest of the writer's fields.
    assert (await _put(store_settings, created_by="carol"))["preferences_required"] is False
    assert (await jobs.get_job(store_settings, TENANT, JOB))["preferences"] == text

    taken = await _put(store_settings, created_by="erin", preferences_required=True)
    fetched = await jobs.get_job(store_settings, TENANT, JOB)
    for row in (taken, fetched):
        assert row is not None
        assert (row["preferences"], row["preferences_updated_at"], row["preferences_updated_by"]) == (
            None,
            None,
            None,
        )
        assert (row["created_by"], row["preferences_required"]) == ("erin", True)
        assert row["health"] == {"overdue": {"since": 1}}, "only the preferences go"

    await jobs.set_preferences(store_settings, TENANT, JOB, text, by="erin", writer="erin")
    cleared = await jobs.set_preferences(store_settings, TENANT, JOB, None, by="erin", writer="erin")
    assert cleared is not None and cleared["preferences"] is None
    assert cleared["preferences_updated_by"] == "erin"


@parametrized
async def test_preferences_land_only_while_the_checked_writer_still_holds_the_job(
    store_settings: Any,
) -> None:
    """The route checks the caller against the job's writer, then writes; a re-save by someone else
    in between makes it their job, and the write -- checked against the old writer -- must not
    land on it, where the text would run on the new writer's scopes."""
    await _put(store_settings, created_by="carol")
    await _put(store_settings, created_by="erin")
    assert await jobs.set_preferences(store_settings, TENANT, JOB, "x", by="carol", writer="carol") is None
    assert (await jobs.get_job(store_settings, TENANT, JOB))["preferences"] is None
    landed = await jobs.set_preferences(store_settings, TENANT, JOB, "y", by="erin", writer="erin")
    assert landed is not None and landed["preferences"] == "y"


@parametrized
async def test_a_shared_subjects_resave_clears_the_preferences(store_settings: Any) -> None:
    """Every holder of a sub-less API key is the subject `api_key`, so its "same writer" re-save
    may be another holder: the text an operator wrote does not carry onto their scopes."""
    from felix.auth.recorded import DEFAULT_API_KEY_SUBJECT as shared

    await _put(store_settings, created_by=shared)
    await jobs.set_preferences(store_settings, TENANT, JOB, "x", by="admin", writer=shared)
    assert (await jobs.get_job(store_settings, TENANT, JOB))["preferences"] == "x"
    assert (await _put(store_settings, created_by=shared))["preferences"] is None


@parametrized
async def test_preferences_never_create_a_job_and_are_capped(store_settings: Any) -> None:
    from felix.jobs.preferences import PREFERENCES_MAX_BYTES, PreferencesTooLong

    assert await jobs.set_preferences(store_settings, TENANT, "gone", "x", by="carol", writer=None) is None
    assert await jobs.get_job(store_settings, TENANT, "gone") is None
    await _put(store_settings)
    assert await jobs.set_preferences(store_settings, OTHER, JOB, "x", by="carol", writer=None) is None
    # Bytes, not characters: `é` is two in UTF-8.
    with pytest.raises(PreferencesTooLong):
        await jobs.set_preferences(
            store_settings, TENANT, JOB, "é" * (PREFERENCES_MAX_BYTES // 2 + 1), by="c", writer=None
        )
    full = "é" * (PREFERENCES_MAX_BYTES // 2)
    written = await jobs.set_preferences(store_settings, TENANT, JOB, full, by="c", writer=None)
    assert written is not None and written["preferences"] == full
