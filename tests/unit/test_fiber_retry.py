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

TENANT = "default"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database_url="memory://fiber-retry",
        object_store="memory",
        allow_insecure=True,
        auth_mode="none",
        environment="development",
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
    async def boom(settings: Any, row: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("connection refused")

    monkeypatch.setattr(fibers, "_run_fiber_step", boom)


async def _pending(settings: Settings) -> str:
    row = await create_fiber(settings, TENANT, state={"steps": [{"op": "complete"}], "cursor": 0})
    return str(row["id"])


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_a_step_that_completes_resets_the_count(
    settings: Settings, clock: _Clock, monkeypatch: Any
) -> None:
    """Failures are consecutive: two before a step that lands, then two more, is two — not
    four — against a ceiling of three."""
    calls = {"n": 0}
    real = fibers._run_fiber_step

    async def flaky(settings: Any, row: dict[str, Any]) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] in {1, 2, 4, 5}:
            raise RuntimeError("transient")
        return await real(settings, row)

    monkeypatch.setattr(fibers, "_run_fiber_step", flaky)
    created = await create_fiber(
        settings,
        TENANT,
        state={"steps": [{"op": "stash", "data": {"k": 1}}, {"op": "complete"}], "cursor": 0},
    )
    fiber_id = str(created["id"])

    for _ in range(5):
        await resume_due_fibers(settings)
        row = await get_fiber(settings, TENANT, fiber_id)
        assert row is not None
        clock.ms = int(row["wake_at"] or clock.ms)

    row = await get_fiber(settings, TENANT, fiber_id)
    assert row is not None
    assert (row["status"], row["attempts"], row["state_json"]["cursor"]) == ("sleeping", 2, 1)
    clock.ms = int(row["wake_at"])
    await resume_due_fibers(settings)
    row = await get_fiber(settings, TENANT, fiber_id)
    assert row is not None
    assert (row["status"], row["attempts"]) == ("completed", 0)


@pytest.mark.asyncio
async def test_a_save_that_keeps_failing_is_still_bounded(
    settings: Settings, clock: _Clock, monkeypatch: Any
) -> None:
    """The failure this exists for: the step ran, its save raised. The count cannot go
    through the save that is failing, so it goes through a write of the columns alone —
    and the fiber is buried at the ceiling like any other, with the error derived from
    the count because the text could not be stored."""

    async def unsaveable(settings: Any, row: dict[str, Any]) -> None:
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


@pytest.mark.asyncio
async def test_a_transient_save_failure_keeps_the_step_done(
    settings: Settings, clock: _Clock, monkeypatch: Any
) -> None:
    """The step's work happened before its save raised (an invoke already called the
    model). The retry persists the advanced cursor and parks: the step is charged, not
    re-run."""
    real = fibers._save_fiber
    calls = {"n": 0}

    async def once(settings: Any, row: dict[str, Any]) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        await real(settings, row)

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


@pytest.mark.asyncio
async def test_a_finished_step_whose_save_failed_stays_finished(
    settings: Settings, clock: _Clock, monkeypatch: Any
) -> None:
    """`complete` ran, then its save raised. Parking it as `sleeping` would re-enter the
    step loop with the cursor past the end and report `completed` over whatever the failed
    save carried; the computed terminal status is kept instead, and nothing is charged."""
    real = fibers._save_fiber
    calls = {"n": 0}

    async def once(settings: Any, row: dict[str, Any]) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        await real(settings, row)

    monkeypatch.setattr(fibers, "_save_fiber", once)
    fiber_id = await _pending(settings)

    await resume_due_fibers(settings)

    row = await get_fiber(settings, TENANT, fiber_id)
    assert row is not None
    assert (row["status"], row["attempts"], row["wake_at"]) == ("completed", 0, None)


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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
