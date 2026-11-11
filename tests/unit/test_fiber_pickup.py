"""A durable run starts within a poll, and one run waiting on a person holds up no other.

Two measured problems on the reference deployment. Submitting a durable run writes a `pending`
fiber and nothing tells the worker, so the only thing that started it was the `* * * * *`
`fiber_scheduler` cron: two runs began at 01:08:53 and 01:14:53, the second the cron fired,
each about 25s after it was submitted. And the sweep stepped its batch one fiber after
another, so the first of those runs — parked 84s on a `write_file` approval — made the sweep
take 89s, with every fiber claimed behind it waiting too.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from felix.config import Settings
from felix.durability import fibers
from felix.durability.fibers import create_fiber, resume_due_fibers, run_fiber_loop


def _settings(**over: Any) -> Settings:
    return Settings(
        database_url="memory://fibers",
        object_store="memory",
        allow_insecure=True,
        auth_mode="none",
        environment="development",
        **{"fiber_poll_seconds": 0.01, "fiber_concurrency": 4, **over},
    )


@pytest.fixture(autouse=True)
def _clean() -> None:
    fibers._memory_fibers.clear()


async def _fiber(settings: Settings, status: str = "pending") -> str:
    row = await create_fiber(
        settings,
        "default",
        status=status,
        state={"steps": [{"op": "complete"}], "cursor": 0},
    )
    return str(row["id"])


def _status(fiber_id: str) -> str:
    return str(fibers._memory_fibers[("default", fiber_id)]["status"])


async def _until(check: Any, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not check():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached before the deadline")
        await asyncio.sleep(0.005)


@pytest.fixture
def blocking(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Make the step of chosen fibers wait on an event — a run parked on an approval."""
    held: set[str] = set()
    started = asyncio.Event()
    release = asyncio.Event()
    original = fibers._run_fiber_step

    async def _step(s: Settings, row: dict, **kw: object) -> dict:
        if str(row["id"]) in held:
            started.set()
            await release.wait()
        return await original(s, row, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(fibers, "_run_fiber_step", _step)
    return {"held": held, "started": started, "release": release}


# --- the loop: pickup latency -----------------------------------------------------


async def test_a_run_submitted_while_the_loop_runs_starts_within_a_poll() -> None:
    settings = _settings()
    stop = asyncio.Event()
    loop = asyncio.create_task(run_fiber_loop(settings, stop))
    try:
        await asyncio.sleep(0.02)  # the loop is idle when the run arrives, as in production
        fiber_id = await _fiber(settings)
        await _until(lambda: _status(fiber_id) == "completed", timeout=0.5)
    finally:
        stop.set()
        await loop


# --- the loop: one parked run holds up nothing else -------------------------------


async def test_a_run_waiting_on_a_person_does_not_hold_up_the_next(blocking: dict[str, Any]) -> None:
    settings = _settings()
    parked = await _fiber(settings)
    blocking["held"].add(parked)
    stop = asyncio.Event()
    loop = asyncio.create_task(run_fiber_loop(settings, stop))
    try:
        await asyncio.wait_for(blocking["started"].wait(), timeout=1)
        behind = await _fiber(settings)
        await _until(lambda: _status(behind) == "completed", timeout=0.5)
        assert _status(parked) == "running", "the parked run finished without being answered"
    finally:
        blocking["release"].set()
        stop.set()
        await loop
    assert _status(parked) == "completed"


async def test_the_loop_claims_no_more_than_it_has_slots_for(blocking: dict[str, Any]) -> None:
    """A full worker leaves the next run unclaimed, so another worker can take it."""
    settings = _settings(fiber_concurrency=1)
    parked = await _fiber(settings)
    blocking["held"].add(parked)
    stop = asyncio.Event()
    loop = asyncio.create_task(run_fiber_loop(settings, stop))
    try:
        await asyncio.wait_for(blocking["started"].wait(), timeout=1)
        waiting = await _fiber(settings)
        await asyncio.sleep(0.05)
        row = fibers._memory_fibers[("default", waiting)]
        assert row["status"] == "pending" and not row.get("lease_owner"), "claimed with no free slot"
        blocking["release"].set()
        await _until(lambda: _status(waiting) == "completed", timeout=0.5)
    finally:
        blocking["release"].set()
        stop.set()
        await loop


async def test_the_loop_survives_a_claim_that_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A store that is down is retried with backoff, not a crashed loop."""
    settings = _settings()
    original = fibers._claim_and_settle
    calls = {"n": 0}

    async def _flaky(s: Settings, *args: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("store unavailable")
        return await original(s, *args)

    monkeypatch.setattr(fibers, "_claim_and_settle", _flaky)
    fiber_id = await _fiber(settings)
    stop = asyncio.Event()
    loop = asyncio.create_task(run_fiber_loop(settings, stop))
    try:
        await _until(lambda: _status(fiber_id) == "completed", timeout=1)
    finally:
        stop.set()
        await loop
    assert calls["n"] >= 2


async def test_stopping_lets_a_running_fiber_finish(blocking: dict[str, Any]) -> None:
    settings = _settings()
    parked = await _fiber(settings)
    blocking["held"].add(parked)
    stop = asyncio.Event()
    loop = asyncio.create_task(run_fiber_loop(settings, stop))
    await asyncio.wait_for(blocking["started"].wait(), timeout=1)
    stop.set()
    await asyncio.sleep(0.02)
    assert not loop.done(), "the loop returned with a fiber still in its step"
    blocking["release"].set()
    await asyncio.wait_for(loop, timeout=1)
    assert _status(parked) == "completed"


# --- the cron sweep: the backstop, now concurrent ---------------------------------


async def test_the_sweep_advances_its_batch_concurrently(blocking: dict[str, Any]) -> None:
    settings = _settings()
    parked = await _fiber(settings)
    behind = await _fiber(settings)
    blocking["held"].add(parked)
    sweep = asyncio.create_task(resume_due_fibers(settings))
    try:
        await asyncio.wait_for(blocking["started"].wait(), timeout=1)
        await _until(lambda: _status(behind) == "completed", timeout=0.5)
    finally:
        blocking["release"].set()
    assert await sweep == 2
    assert _status(parked) == "completed"


# --- the cron sweep: it claims what it can start, out of the worker's slots --------


async def test_the_sweep_claims_no_more_than_it_can_start(blocking: dict[str, Any]) -> None:
    """A claim is a lease, and only a fiber in its step renews one.

    The sweep claimed its whole batch and started `fiber_concurrency` of it, so the rest held a
    lease from the moment they were queued behind a slot: five minutes later it had lapsed under
    a holder that had not begun, and the fiber was claimed and run by someone else as well.
    """
    settings = _settings(fiber_concurrency=1)
    parked = await _fiber(settings)
    waiting = await _fiber(settings)
    blocking["held"].add(parked)
    sweep = asyncio.create_task(resume_due_fibers(settings))
    try:
        await asyncio.wait_for(blocking["started"].wait(), timeout=1)
        await asyncio.sleep(0.02)
        row = fibers._memory_fibers[("default", waiting)]
        assert (row["status"], row.get("lease_until")) == ("pending", None), "claimed with no slot to run in"
    finally:
        blocking["release"].set()
    # And it is not left for the next tick: the sweep claims it once the slot is free.
    assert await asyncio.wait_for(sweep, timeout=1) == 2
    assert (_status(parked), _status(waiting)) == ("completed", "completed")


async def test_the_sweep_and_the_loop_share_one_workers_slots(blocking: dict[str, Any]) -> None:
    """`fiber_concurrency` is per worker. The loop and the sweep each counted their own, so a
    worker whose loop was full still started that many again from the sweep."""
    settings = _settings(fiber_concurrency=1)
    parked = await _fiber(settings)
    blocking["held"].add(parked)
    stop = asyncio.Event()
    loop = asyncio.create_task(run_fiber_loop(settings, stop))
    try:
        await asyncio.wait_for(blocking["started"].wait(), timeout=1)
        waiting = await _fiber(settings)
        assert await asyncio.wait_for(resume_due_fibers(settings), timeout=1) == 0
        row = fibers._memory_fibers[("default", waiting)]
        assert (row["status"], row.get("lease_until")) == ("pending", None), "the sweep ran a second fiber"
    finally:
        blocking["release"].set()
        stop.set()
        await loop


async def test_a_fiber_that_arrives_during_a_sweep_is_not_that_sweeps(blocking: dict[str, Any]) -> None:
    """The sweep is over what was due when it began. A workflow's orchestrator starts its step's
    fiber mid-sweep; claimed by the same sweep, a step waiting on a person would hold it open."""
    settings = _settings()
    parked = await _fiber(settings)
    blocking["held"].add(parked)
    sweep = asyncio.create_task(resume_due_fibers(settings))
    try:
        await asyncio.wait_for(blocking["started"].wait(), timeout=1)
        late = await _fiber(settings)
    finally:
        blocking["release"].set()
    assert await asyncio.wait_for(sweep, timeout=1) == 1
    assert _status(late) == "pending"


# --- the cron sweep and a fiber the claim buries -----------------------------------


async def _lost_for_good(settings: Settings) -> str:
    """A fiber one lost worker short of its limit, its last holder's lease run out."""
    fiber_id = await _fiber(settings)
    fibers._memory_fibers[("default", fiber_id)].update(
        status="running",
        lease_owner="a-worker-that-died",
        lease_until=fibers.now_ms() - 1,
        state_json={
            "steps": [{"op": "complete"}],
            "cursor": 0,
            fibers.FIBER_LAPSES_KEY: settings.fiber_max_attempts - 1,
        },
    )
    return fiber_id


async def test_a_sweep_goes_on_past_a_claim_that_only_buried() -> None:
    """A buried fiber starts nothing, and "nothing started" was how the sweep knew it was done:
    it stopped with due fibers it had not tried."""
    settings = _settings(fiber_concurrency=1)
    buried = await _lost_for_good(settings)
    behind = await _fiber(settings)

    assert await asyncio.wait_for(resume_due_fibers(settings), timeout=1) == 1

    assert (_status(buried), _status(behind)) == ("dead", "completed")


async def test_what_follows_a_burial_waits_for_the_fibers_claimed_beside_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run-finished hook may take ten seconds. Run before the claimed fibers were started, it
    held each of them under a lease nobody was renewing yet."""
    settings = _settings()
    buried = await _lost_for_good(settings)
    beside = await _fiber(settings)
    original = fibers._after_terminal
    beside_when_the_hook_ended: list[str] = []

    async def slow_hook(s: Settings, row: dict[str, Any]) -> None:
        if row["id"] == buried:
            await asyncio.sleep(0.05)
            beside_when_the_hook_ended.append(_status(beside))
        await original(s, row)

    monkeypatch.setattr(fibers, "_after_terminal", slow_hook)

    await asyncio.wait_for(resume_due_fibers(settings), timeout=2)

    assert beside_when_the_hook_ended == ["completed"]


async def test_a_burials_follow_up_that_raises_orphans_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """It raised out of the claim, after the claim had committed: every fiber claimed in that
    transaction was left leased with no task to run it."""
    settings = _settings()
    buried = await _lost_for_good(settings)
    beside = await _fiber(settings)
    original = fibers._after_terminal

    async def broken(s: Settings, row: dict[str, Any]) -> None:
        if row["id"] == buried:
            raise RuntimeError("a run view that could not be built")
        await original(s, row)

    monkeypatch.setattr(fibers, "_after_terminal", broken)

    assert await asyncio.wait_for(resume_due_fibers(settings), timeout=2) == 1

    assert (_status(buried), _status(beside)) == ("dead", "completed")
