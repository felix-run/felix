"""A fiber step that keeps raising is retried with backoff, then buried.

Before this a step that raised outside the invoke's own handler was released and re-claimed
on the next tick, once a minute, until `expires_at` — no counter, no backoff, no terminal
state. The failure injected here is `_run_fiber_step` itself raising, which is what a save
that cannot land or a store that is down looks like to the scheduler.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.config import Settings
from felix.durability import fibers
from felix.durability.fibers import (
    FIBER_RETRY_BASE_MS,
    FIBER_RETRY_MAX_MS,
    create_fiber,
    get_fiber,
    resume_due_fibers,
    retry_delay_ms,
)
from felix.durability.runs import get_durable_run

from tests.support.factories import make_settings

TENANT = "default"


@pytest.fixture
def settings() -> Settings:
    return make_settings(
        fiber_max_attempts=3,
    )


@pytest.fixture(autouse=True)
def _clean() -> None:
    fibers.reset_memory_fibers()


class _Clock:
    def __init__(self, ms: int) -> None:
        self.ms = ms


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    c = _Clock(1_800_000_000_000)
    monkeypatch.setattr(fibers, "now_ms", lambda: c.ms)
    return c


def _always_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(settings: Any, row: dict[str, Any], **kw: Any) -> dict[str, Any]:
        raise RuntimeError("connection refused")

    monkeypatch.setattr(fibers, "_run_fiber_step", boom)


async def _pending(settings: Settings) -> str:
    row = await create_fiber(settings, TENANT, state={"steps": [{"op": "complete"}], "cursor": 0})
    return str(row["id"])


async def test_a_failing_step_backs_off_and_counts(
    settings: Settings, clock: _Clock, monkeypatch: Any
) -> None:
    _always_raise(monkeypatch)
    fiber_id = await _pending(settings)

    assert await resume_due_fibers(settings) == 1
    row = await get_fiber(settings, TENANT, fiber_id)
    assert row is not None
    assert (row["status"], row["attempts"]) == ("sleeping", 1)
    assert row["wake_at"] == clock.ms + FIBER_RETRY_BASE_MS
    assert row["lease_until"] is None, "a parked fiber must not hold the claim"

    assert await resume_due_fibers(settings) == 0, "not due yet: the backoff is the point"

    clock.ms = row["wake_at"]
    assert await resume_due_fibers(settings) == 1
    row = await get_fiber(settings, TENANT, fiber_id)
    assert row is not None
    assert (row["status"], row["attempts"]) == ("sleeping", 2)
    assert row["wake_at"] == clock.ms + 2 * FIBER_RETRY_BASE_MS, "the delay doubles per failure"


async def test_at_the_ceiling_the_fiber_is_dead_and_never_claimed_again(
    settings: Settings, clock: _Clock, monkeypatch: Any
) -> None:
    _always_raise(monkeypatch)
    fiber_id = await _pending(settings)

    for _ in range(3):
        await resume_due_fibers(settings)
        row = await get_fiber(settings, TENANT, fiber_id)
        assert row is not None
        clock.ms = int(row["wake_at"] or clock.ms)

    row = await get_fiber(settings, TENANT, fiber_id)
    assert row is not None
    assert (row["status"], row["attempts"], row["wake_at"]) == ("dead", 3, None)
    clock.ms += FIBER_RETRY_MAX_MS
    assert await resume_due_fibers(settings) == 0, "a dead fiber was claimed"

    run = await get_durable_run(settings, TENANT, fiber_id)
    assert run is not None
    assert run["status"] == "dead"
    assert "step failed 3 times" in run["error"] and "connection refused" in run["error"]


async def test_a_step_that_completes_resets_the_count(
    settings: Settings, clock: _Clock, monkeypatch: Any
) -> None:
    """Failures are consecutive: two before a step that lands, then two more, is two — not
    four — against a ceiling of three.

    Asserted as the property rather than as a sweep count, because the count is no longer a
    property of the rule. A claim runs the fiber to its next suspension, so the step that
    lands and the failure after it now share one sweep — which is precisely the case that
    makes the reset load-bearing: charging the pre-sweep streak there would bury a fiber that
    had just made progress. The peak is what the ceiling is compared against, so the peak is
    what this watches.
    """
    calls = {"n": 0}
    real = fibers._run_fiber_step

    async def flaky(settings: Any, row: dict[str, Any], **kw: Any) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] in {1, 2, 4, 5}:
            raise RuntimeError("transient")
        return await real(settings, row, **kw)

    monkeypatch.setattr(fibers, "_run_fiber_step", flaky)
    created = await create_fiber(
        settings,
        TENANT,
        state={"steps": [{"op": "stash", "data": {"k": 1}}, {"op": "complete"}], "cursor": 0},
    )
    fiber_id = str(created["id"])

    seen: list[int] = []
    for _ in range(8):
        await resume_due_fibers(settings)
        row = await get_fiber(settings, TENANT, fiber_id)
        assert row is not None
        seen.append(int(row["attempts"] or 0))
        if row["status"] in {"completed", "dead"}:
            break
        clock.ms = int(row["wake_at"] or clock.ms)

    assert row["status"] == "completed", f"the fiber did not finish: {row['status']} after {seen}"
    assert row["attempts"] == 0, "a completed fiber still carries a failure count"
    # Four failures were injected, and the ceiling is three. The fiber survives only because
    # the streak resets — so the number that matters is the peak, not the total.
    assert max(seen) == 2, f"the streak did not reset; attempts went {seen}"
    assert seen.count(1) >= 2, f"the count never restarted, so nothing reset: {seen}"
    assert calls["n"] >= 5, "not every injected failure was reached"


async def test_a_save_that_keeps_failing_is_still_bounded(
    settings: Settings, clock: _Clock, monkeypatch: Any
) -> None:
    """The failure this exists for: the step ran, its save raised. The count cannot go
    through the save that is failing, so it goes through a write of the columns alone —
    and the fiber is buried at the ceiling like any other, with the error derived from
    the count because the text could not be stored."""

    async def unsaveable(settings: Any, row: dict[str, Any], **kw: Any) -> None:
        raise RuntimeError("state_json is not JSON serialisable")

    monkeypatch.setattr(fibers, "_save_fiber", unsaveable)
    # A step that is not the last: `complete` would finish the run, and a finished step
    # whose save failed keeps its status (see the test below) rather than being retried.
    created = await create_fiber(
        settings, TENANT, state={"steps": [{"op": "stash", "data": {}}, {"op": "complete"}], "cursor": 0}
    )
    fiber_id = str(created["id"])

    for n in (1, 2, 3):
        assert await resume_due_fibers(settings) == 1
        row = await get_fiber(settings, TENANT, fiber_id)
        assert row is not None
        assert row["attempts"] == n, "an attempt was lost because its save failed"
        assert row["lease_until"] is None
        clock.ms = int(row["wake_at"] or clock.ms)

    assert (row["status"], row["wake_at"]) == ("dead", None)
    assert await resume_due_fibers(settings) == 0
    run = await get_durable_run(settings, TENANT, fiber_id)
    assert run is not None and run["status"] == "dead" and "step failed 3 times" in run["error"]


async def test_a_save_that_raises_leaves_the_holder_its_claim(monkeypatch: Any) -> None:
    """What the test above cannot see, because it replaces the save whole.

    The fallbacks that run when a save raises -- `_record_attempt`, then `_release_fiber` -- are
    scoped to the claim the row was handed, which is its `lease_until`. A save that cleared the
    row's lease before writing would leave them a row naming no claim: nothing recorded, nothing
    released, and the fiber stranded under its lease with the failure uncounted.
    """

    def store_down(**_kw: Any) -> Any:
        raise ConnectionError("store down")

    monkeypatch.setattr(fibers, "get_session_factory", store_down)
    on_postgres = make_settings(database_url="postgresql+psycopg://u@127.0.0.1:9/none")
    row = {
        "tenant_id": TENANT,
        "id": "f",
        "status": "sleeping",
        "state_json": {},
        "version": 3,
        "lease_owner": "worker-a",
        "lease_until": 1234,
    }

    with pytest.raises(ConnectionError):
        await fibers._save_fiber(on_postgres, row)

    assert (row["lease_owner"], row["lease_until"], row["version"]) == ("worker-a", 1234, 3)


async def test_a_step_that_saved_unheard_gives_its_claim_up(settings: Settings, monkeypatch: Any) -> None:
    """The step's save committed and its answer was lost, so the step raised.

    The store's row is then a version ahead of the one the holder carries, under the holder's own
    lease. The retry write lost its compare-and-set -- which was reported as saved -- and neither
    fallback ran, so the fiber sat under that lease until it lapsed and was then counted as a
    worker lost. The holder lets go instead, and the fiber goes on from what the step saved.
    """
    created = await create_fiber(
        settings, TENANT, state={"steps": [{"op": "stash", "data": {}}, {"op": "complete"}], "cursor": 0}
    )
    fiber_id = str(created["id"])
    real = fibers._run_fiber_step

    async def saves_unheard(s: Any, row: dict[str, Any], **kw: Any) -> dict[str, Any]:
        await real(s, dict(row), **kw)  # lands in the store; the holder's own row never hears
        raise ConnectionError("the answer was lost")

    monkeypatch.setattr(fibers, "_run_fiber_step", saves_unheard)
    (row,) = await fibers._claim_due(settings)
    await fibers._advance_claimed(settings, row)

    stored = await get_fiber(settings, TENANT, fiber_id)
    assert stored is not None
    assert (stored["status"], stored["lease_owner"], stored["lease_until"]) == ("running", "", None), (
        "the fiber was stranded under a lease nobody holds"
    )
    assert (stored["state_json"]["cursor"], stored["attempts"]) == (1, 0), "the step that landed was undone"


async def test_a_retry_write_that_was_discarded_is_not_reported_saved(settings: Settings) -> None:
    """`_save_fiber` reports a lost compare-and-set by returning. Read as "saved", an unsaved
    `dead` was announced as the run's end while the store held something else."""
    created = await create_fiber(settings, TENANT, state={"steps": [{"op": "complete"}], "cursor": 0})
    fiber_id = str(created["id"])
    (row,) = await fibers._claim_due(settings)
    await fibers.stop_fiber(settings, TENANT, fiber_id, "stopped by its workflow")

    saved = await fibers._retry_or_dead(settings, row, settings.fiber_max_attempts, RuntimeError("boom"))

    stored = await get_fiber(settings, TENANT, fiber_id)
    assert stored is not None
    assert (saved, row["status"], stored["status"]) == (False, "dead", "failed")


async def test_a_transient_save_failure_keeps_the_step_done(
    settings: Settings, clock: _Clock, monkeypatch: Any
) -> None:
    """The step's work happened before its save raised (an invoke already called the
    model). The retry persists the advanced cursor and parks: the step is charged, not
    re-run."""
    real = fibers._save_fiber
    calls = {"n": 0}

    async def once(settings: Any, row: dict[str, Any], **kw: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        await real(settings, row, **kw)

    monkeypatch.setattr(fibers, "_save_fiber", once)
    created = await create_fiber(
        settings,
        TENANT,
        state={"steps": [{"op": "stash", "data": {"k": 1}}, {"op": "complete"}], "cursor": 0},
    )
    fiber_id = str(created["id"])

    await resume_due_fibers(settings)

    row = await get_fiber(settings, TENANT, fiber_id)
    assert row is not None
    assert (row["status"], row["attempts"], row["state_json"]["cursor"]) == ("sleeping", 1, 1)
    assert row["state_json"]["stash"] == {"k": 1}, "the completed step's effect was lost"


async def test_a_finished_step_whose_save_failed_stays_finished(
    settings: Settings, clock: _Clock, monkeypatch: Any
) -> None:
    """`complete` ran, then its save raised. Parking it as `sleeping` would re-enter the
    step loop with the cursor past the end and report `completed` over whatever the failed
    save carried; the computed terminal status is kept instead, and nothing is charged."""
    real = fibers._save_fiber
    calls = {"n": 0}

    async def once(settings: Any, row: dict[str, Any], **kw: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        await real(settings, row, **kw)

    monkeypatch.setattr(fibers, "_save_fiber", once)
    fiber_id = await _pending(settings)

    await resume_due_fibers(settings)

    row = await get_fiber(settings, TENANT, fiber_id)
    assert row is not None
    assert (row["status"], row["attempts"], row["wake_at"]) == ("completed", 0, None)


async def test_a_failing_step_leaves_no_heartbeat_behind(
    settings: Settings, clock: _Clock, monkeypatch: Any
) -> None:
    import asyncio

    _always_raise(monkeypatch)
    await _pending(settings)

    await resume_due_fibers(settings)

    beats = [
        t for t in asyncio.all_tasks() if t.get_coro().__qualname__.endswith("_heartbeat") and not t.done()
    ]
    assert not beats, "the lease renewal task outlived its failed step"


async def test_a_parked_fiber_past_its_expiry_expires_on_wake(
    settings: Settings, clock: _Clock, monkeypatch: Any
) -> None:
    """Backoffs sum past a durable chat's `expires_at`; the wake must honour expiry before
    it retries, so a run does not come back from the dead after its token is gone."""
    _always_raise(monkeypatch)
    created = await create_fiber(
        settings,
        TENANT,
        state={"steps": [{"op": "complete"}], "cursor": 0, "expires_at": clock.ms + FIBER_RETRY_BASE_MS // 2},
    )
    fiber_id = str(created["id"])
    await resume_due_fibers(settings)
    row = await get_fiber(settings, TENANT, fiber_id)
    assert row is not None and row["status"] == "sleeping"
    monkeypatch.undo()  # the step runs for real from here; the clock patch goes too
    monkeypatch.setattr(fibers, "now_ms", lambda: int(row["wake_at"]))

    await resume_due_fibers(settings)

    row = await get_fiber(settings, TENANT, fiber_id)
    assert row is not None
    assert row["status"] == "expired"


def test_backoff_doubles_from_the_base_to_the_cap() -> None:
    assert [retry_delay_ms(n) for n in (1, 2, 3)] == [
        FIBER_RETRY_BASE_MS,
        2 * FIBER_RETRY_BASE_MS,
        4 * FIBER_RETRY_BASE_MS,
    ]
    assert retry_delay_ms(40) == FIBER_RETRY_MAX_MS


def test_retention_sweeps_every_status_the_store_calls_terminal() -> None:
    """A `dead` fiber kept forever is the retention gap re-opened one status at a time."""
    from felix.durability.fibers import FIBER_TERMINAL_STATUSES
    from felix.jobs.retention import TERMINAL_FIBER_STATUSES

    assert TERMINAL_FIBER_STATUSES == FIBER_TERMINAL_STATUSES
    assert "dead" in TERMINAL_FIBER_STATUSES
