"""A job whose run is still going is not fired again.

`claim_run` compare-and-set only `next_run_at`, and moved it to the next slot as it claimed. It wrote
`last_status="running"` and never read it, so a run that outlasted its interval found its next slot
due on a later tick and was fired a second time beside itself, on the same thread. Now the claim
reads the mark -- unless it is older than `RUNNING_STALE_MS`, so a worker that died mid-run does not
stop its job for good -- and every way a run ends clears it.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from felix.config import Settings
from felix.jobs import scheduler
from felix.jobs import store as jobs_store

from tests.support.factories import make_settings

TENANT = "default"
T0 = 1_800_000_000_000
MINUTE = 60_000


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture(autouse=True)
def _clean() -> Any:
    jobs_store.reset_jobs_for_tests()
    yield
    jobs_store.reset_jobs_for_tests()


async def _job(settings: Settings, name: str = "a") -> None:
    await jobs_store.put_job(settings, TENANT, name, schedule="* * * * *", manifest_id="m", payload={})


async def _row(settings: Settings, name: str = "a") -> dict[str, Any]:
    row = await jobs_store.get_job(settings, TENANT, name)
    assert row is not None
    return row


def test_the_staleness_bound_outlasts_the_longest_legitimate_run() -> None:
    """A run lasts at most its wall clock plus one approval wait begun at its end; the bound must
    clear both with room, or a slow but healthy run is fired over."""
    from felix.manifests.schema import ABSOLUTE_LIMITS, MAX_INTEGRATION_TIMEOUT_S

    longest_ms = (ABSOLUTE_LIMITS["max_wall_clock_seconds"] + MAX_INTEGRATION_TIMEOUT_S) * 1000
    assert 2 * longest_ms <= jobs_store.RUNNING_STALE_MS


async def test_a_slow_run_is_not_fired_again_by_the_next_sweep(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real shape: sweep one is still inside the job when a minute later sweep two runs."""
    await _job(settings)
    clock = {"now": T0}
    monkeypatch.setattr(scheduler, "now_ms", lambda: clock["now"])
    release, entered = asyncio.Event(), asyncio.Event()
    calls: list[int] = []

    async def _slow(_settings: Settings, *, tenant_id: str, job: dict[str, Any]) -> dict[str, Any]:
        calls.append(clock["now"])
        entered.set()
        await release.wait()
        return {"status": "ok"}

    monkeypatch.setattr(scheduler, "_invoke_job_manifest", _slow)
    first = asyncio.create_task(scheduler.run_due_jobs(settings))
    await asyncio.wait_for(entered.wait(), timeout=5)
    assert calls == [T0]
    assert (await _row(settings))["last_status"] == "running"

    clock["now"] = T0 + 5 * MINUTE  # five slots later: the claimed slot is long due
    assert await asyncio.wait_for(scheduler.run_due_jobs(settings), timeout=5) == 0
    assert calls == [T0], "the job was fired again while its first run was going"

    release.set()
    assert await asyncio.wait_for(first, timeout=5) == 1
    assert (await _row(settings))["last_status"] == "ok"
    # Ended, the due slot it was held off is fired once, by the next sweep.
    assert await asyncio.wait_for(scheduler.run_due_jobs(settings), timeout=5) == 1
    assert len(calls) == 2


async def test_a_stale_running_mark_is_reclaimed(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """A worker that died mid-run never clears its mark. Past the bound the job runs again."""
    await _job(settings)
    seen = (await _row(settings))["next_run_at"]
    assert await jobs_store.claim_run(
        settings, TENANT, "a", seen_next_run_at=seen, last_run_at=T0, next_run_at=T0 + MINUTE
    )
    monkeypatch.setattr(scheduler, "_invoke_job_manifest", _ok)

    monkeypatch.setattr(scheduler, "now_ms", lambda: T0 + jobs_store.RUNNING_STALE_MS)
    assert await scheduler.run_due_jobs(settings) == 0, "reclaimed at the bound, not past it"
    monkeypatch.setattr(scheduler, "now_ms", lambda: T0 + jobs_store.RUNNING_STALE_MS + 1)
    assert await scheduler.run_due_jobs(settings) == 1
    assert (await _row(settings))["last_status"] == "ok"


async def _ok(_settings: Settings, *, tenant_id: str, job: dict[str, Any]) -> dict[str, Any]:
    return {"status": "ok"}


async def test_a_run_that_fails_to_record_still_clears_its_mark(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The outer handler in `fire_job`: the run record raised, and the job used to keep `running`."""
    await _job(settings)
    seen = (await _row(settings))["next_run_at"]
    assert await jobs_store.claim_run(
        settings, TENANT, "a", seen_next_run_at=seen, last_run_at=T0, next_run_at=None
    )
    monkeypatch.setattr(scheduler, "_invoke_job_manifest", _ok)
    real = jobs_store.record_run
    failures = iter([True])

    async def _flaky(*a: Any, **kw: Any) -> dict[str, Any]:
        if next(failures, False):
            raise RuntimeError("store down")
        return await real(*a, **kw)

    monkeypatch.setattr(jobs_store, "record_run", _flaky)
    run = await scheduler.fire_job(settings, TENANT, await _row(settings), started_at=T0)
    assert (run["status"], run["error"]) == ("error", "execution failed")
    row = await _row(settings)
    assert (row["last_status"], row["last_error"]) == ("error", "execution failed")


async def test_a_cancelled_run_clears_its_mark(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    await _job(settings)
    seen = (await _row(settings))["next_run_at"]
    assert await jobs_store.claim_run(
        settings, TENANT, "a", seen_next_run_at=seen, last_run_at=T0, next_run_at=None
    )
    started = asyncio.Event()

    async def _hang(_settings: Settings, *, tenant_id: str, job: dict[str, Any]) -> dict[str, Any]:
        started.set()
        await asyncio.sleep(3600)
        return {"status": "ok"}

    monkeypatch.setattr(scheduler, "_invoke_job_manifest", _hang)
    task = asyncio.create_task(scheduler.fire_job(settings, TENANT, await _row(settings), started_at=T0))
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    row = await _row(settings)
    assert (row["last_status"], row["last_error"]) == ("error", "cancelled")
    # And it leaves a run record: the model may already have been called, and a heartbeat's daily
    # cap counts records -- a cancelled firing that left none was spend nothing counted.
    (run,) = await jobs_store.list_runs(settings, TENANT, "a")
    assert (run["status"], run["error"], run["started_at"]) == ("error", "cancelled", T0)
    assert await jobs_store.count_runs_since(settings, TENANT, "a", T0) == 1


async def test_a_run_cancelled_after_its_record_is_not_recorded_twice(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancelled between the run record and the end of its claim: one record, not two."""
    await _job(settings)
    seen = (await _row(settings))["next_run_at"]
    assert await jobs_store.claim_run(
        settings, TENANT, "a", seen_next_run_at=seen, last_run_at=T0, next_run_at=None
    )
    monkeypatch.setattr(scheduler, "_invoke_job_manifest", _ok)
    real_finish = jobs_store.finish_run
    calls = iter([True])

    async def _cancel_once(*a: Any, **kw: Any) -> bool:
        if next(calls, False):
            raise asyncio.CancelledError
        return await real_finish(*a, **kw)

    monkeypatch.setattr(jobs_store, "finish_run", _cancel_once)
    with pytest.raises(asyncio.CancelledError):
        await scheduler.fire_job(settings, TENANT, await _row(settings), started_at=T0)
    runs = await jobs_store.list_runs(settings, TENANT, "a")
    assert [r["status"] for r in runs] == ["ok"]
    assert (await _row(settings))["last_status"] == "error", "its mark still cleared"


async def test_a_failed_run_is_recorded_before_its_mark_clears(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Freed first, a manual run could claim the job in the gap and count one run fewer than ran."""
    await _job(settings)
    seen = (await _row(settings))["next_run_at"]
    assert await jobs_store.claim_run(
        settings, TENANT, "a", seen_next_run_at=seen, last_run_at=T0, next_run_at=None
    )
    monkeypatch.setattr(scheduler, "_invoke_job_manifest", _ok)
    real = jobs_store.record_run
    attempts: list[str] = []

    async def _record(*a: Any, **kw: Any) -> dict[str, Any]:
        attempts.append((await _row(settings))["last_status"])
        if len(attempts) == 1:
            raise RuntimeError("store down")
        return await real(*a, **kw)

    monkeypatch.setattr(jobs_store, "record_run", _record)
    run = await scheduler.fire_job(settings, TENANT, await _row(settings), started_at=T0)
    assert run["error"] == "execution failed"
    assert attempts == ["running", "running"], "the failure was recorded after the mark was cleared"
    assert (await _row(settings))["last_status"] == "error"


async def test_a_manual_claim_refuses_a_running_job_and_holds_off_the_sweep(settings: Settings) -> None:
    await _job(settings)
    assert await jobs_store.claim_manual_run(settings, TENANT, "a", last_run_at=T0)
    assert not await jobs_store.claim_manual_run(settings, TENANT, "a", last_run_at=T0 + MINUTE)
    row = await _row(settings)
    assert row["next_run_at"] is None, "a manual claim leaves the schedule alone"
    assert not await jobs_store.claim_run(
        settings, TENANT, "a", seen_next_run_at=None, last_run_at=T0 + MINUTE, next_run_at=T0 + 2 * MINUTE
    ), "the sweep fired over a manual run"
    assert not await jobs_store.claim_manual_run(settings, TENANT, "gone", last_run_at=T0)


async def test_a_run_reclaimed_as_stale_does_not_erase_the_reclaimers_mark(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run A outlives the bound and B reclaims the job. A's end used to write unconditionally and
    clear B's `running`, and the next tick fired a third run beside B."""
    await _job(settings)
    seen = (await _row(settings))["next_run_at"]
    assert await jobs_store.claim_run(
        settings, TENANT, "a", seen_next_run_at=seen, last_run_at=T0, next_run_at=T0 + MINUTE
    )
    b_at = T0 + jobs_store.RUNNING_STALE_MS + 1
    assert await jobs_store.claim_run(
        settings, TENANT, "a", seen_next_run_at=T0 + MINUTE, last_run_at=b_at, next_run_at=b_at + MINUTE
    )
    monkeypatch.setattr(scheduler, "_invoke_job_manifest", _ok)
    job = await _row(settings)

    await scheduler.fire_job(settings, TENANT, job, started_at=T0)  # A ends
    row = await _row(settings)
    assert (row["last_status"], row["last_run_at"]) == ("running", b_at), "A's end erased B's claim"
    assert not await jobs_store.claim_run(
        settings, TENANT, "a", seen_next_run_at=b_at + MINUTE, last_run_at=b_at + 2 * MINUTE, next_run_at=None
    )

    await scheduler.fire_job(settings, TENANT, job, started_at=b_at)  # B ends
    assert (await _row(settings))["last_status"] == "ok"


async def test_disabling_a_job_clears_a_running_mark_and_reenabling_lets_it_fire(settings: Settings) -> None:
    """The operator's kill switch for a worker that died mid-run: no 6-hour wait."""
    await _job(settings)
    seen = (await _row(settings))["next_run_at"]
    assert await jobs_store.claim_run(
        settings, TENANT, "a", seen_next_run_at=seen, last_run_at=T0, next_run_at=T0
    )

    async def _put(enabled: bool) -> None:
        await jobs_store.put_job(
            settings, TENANT, "a", schedule="* * * * *", manifest_id="m", payload={}, enabled=enabled
        )

    await _put(True)
    assert (await _row(settings))["last_status"] == "running", "an ordinary re-save keeps the mark"
    await _put(False)
    row = await _row(settings)
    assert (row["last_status"], row["last_error"]) == ("error", jobs_store.DISABLED_WHILE_RUNNING)
    await _put(True)
    assert await jobs_store.claim_run(
        settings, TENANT, "a", seen_next_run_at=T0, last_run_at=T0 + MINUTE, next_run_at=None
    )
