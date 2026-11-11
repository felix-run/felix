"""A step stops when its worker's claim on the fiber is gone (felix-run/felix#531).

The heartbeat that renews a claim gave up after one failed renewal, and a renewal that matched no
row -- the claim had lapsed and another worker had taken the fiber -- looked like success. Either
way the step went on running beside the new owner's: the same `invoke`, the same tool calls,
both sets of side effects. Now a renewal that raises is retried while the lease still stands, a
claim found gone stops the step at once, and a lost claim is left to its new owner -- nothing
charged, parked or released on its row.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import pytest
from felix.config import Settings
from felix.durability import fibers as F

from tests.support.factories import make_settings


def _settings() -> Settings:
    return make_settings(host="127.0.0.1")


async def _claimed(settings: Settings) -> dict[str, Any]:
    await F.create_fiber(
        settings,
        "t",
        kind="durable_chat",
        status="pending",
        state={"steps": [{"op": "invoke"}], "cursor": 0, "stash": {}},
    )
    (row,) = await F._claim_due_memory(settings, F.now_ms())
    return row


def _stored(row: dict[str, Any]) -> dict[str, Any]:
    return F._memory_fibers[(row["tenant_id"], row["id"])]


class _Step:
    """A step that runs until released, recording whether it was cancelled instead."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = False

    async def __call__(self, settings: Any, row: dict[str, Any], **_kw: Any) -> dict[str, Any]:
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        row["status"] = "completed"
        row["version"] = int(row.get("version") or 0) + 1
        return row


async def test_a_step_stops_when_another_worker_takes_its_fiber(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings()
    row = await _claimed(settings)
    step = _Step()
    monkeypatch.setattr(F, "_run_fiber_step", step)
    monkeypatch.setattr(F, "FIBER_LEASE_RENEW_MS", 10)

    advancing = asyncio.create_task(F._advance_claimed(settings, row))
    await asyncio.wait_for(step.started.wait(), timeout=5)
    # The claim lapsed and another worker took it.
    _stored(row)["lease_owner"] = "worker-b"
    await asyncio.wait_for(advancing, timeout=5)

    assert step.cancelled, "the step ran on beside the worker that now holds the fiber"
    stored = _stored(row)
    # Its row is the new owner's: no attempt charged, not parked, not released.
    assert (stored["lease_owner"], stored["attempts"], stored["status"]) == ("worker-b", 0, "running")


async def test_a_renewal_that_fails_once_is_retried_rather_than_abandoned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    row = await _claimed(settings)
    step = _Step()
    monkeypatch.setattr(F, "_run_fiber_step", step)
    monkeypatch.setattr(F, "FIBER_LEASE_RENEW_MS", 10)
    real = F._renew_lease
    calls = 0
    renewed_after_blip = asyncio.Event()

    async def flaky(settings: Any, row: dict[str, Any]) -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionError("store blip")
        renewed_after_blip.set()
        return await real(settings, row)

    monkeypatch.setattr(F, "_renew_lease", flaky)

    advancing = asyncio.create_task(F._advance_claimed(settings, row))
    await asyncio.wait_for(step.started.wait(), timeout=5)
    await asyncio.wait_for(renewed_after_blip.wait(), timeout=5)
    step.release.set()
    await asyncio.wait_for(advancing, timeout=5)

    assert not step.cancelled, "one failed renewal stopped a step whose lease still stood"
    assert calls >= 2, "the heartbeat stopped renewing after the failure"


async def test_a_lease_that_cannot_be_renewed_stops_the_step_before_it_lapses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Giving up one interval early is the point: once the lease lapses another worker may
    already be running the same step, and stopping after that is too late."""
    settings = _settings()
    step = _Step()
    monkeypatch.setattr(F, "_run_fiber_step", step)
    # Before the claim: the heartbeat counts down the lease the claim wrote.
    monkeypatch.setattr(F, "FIBER_LEASE_MS", 300)
    monkeypatch.setattr(F, "FIBER_LEASE_RENEW_MS", 100)
    row = await _claimed(settings)
    started = F.now_ms()

    async def down(*_a: Any, **_k: Any) -> bool:
        raise ConnectionError("store down")

    monkeypatch.setattr(F, "_renew_lease", down)

    await asyncio.wait_for(F._advance_claimed(settings, row), timeout=5)

    assert step.cancelled
    assert F.now_ms() - started < 300, "the step outlived the lease it could not renew"


# --- a claim cut short by the worker stopping ----------------------------------------------------


async def test_a_cancelled_claim_stops_its_step_and_then_gives_the_fiber_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worker's drain ran out and the fiber's task was cancelled.

    The cancel landed on the wait *for* the step, which cancels nothing it waits on: the step
    ran on with no task behind it, and the claim was released on the way out -- so another worker
    took the fiber at once and ran the same turn beside it. Keeping the lease instead was no
    answer: every run in flight at a worker's stop then sat out a whole lease, and was counted as
    a worker lost. The step is stopped first, and the claim given back once it has.
    """
    settings = _settings()
    row = await _claimed(settings)
    held = row["lease_until"]
    lease_when_the_step_ended: list[Any] = []

    class _Watched(_Step):
        async def __call__(self, settings: Any, row: dict[str, Any], **kw: Any) -> dict[str, Any]:
            try:
                return await super().__call__(settings, row, **kw)
            finally:
                lease_when_the_step_ended.append(_stored(row)["lease_until"])

    step = _Watched()
    monkeypatch.setattr(F, "_run_fiber_step", step)

    advancing = asyncio.create_task(F._advance_claimed(settings, row))
    await asyncio.wait_for(step.started.wait(), timeout=5)
    advancing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(advancing, timeout=5)

    assert step.cancelled, "the step outlived the task that was running it"
    assert lease_when_the_step_ended == [held], "the claim was given up under a step still running"
    stored = _stored(row)
    assert (stored["status"], stored["lease_owner"], stored["lease_until"]) == ("running", "", None), (
        "the fiber was left under a lease nobody will renew"
    )
    # Claimable at once, and not as a fiber whose worker was lost.
    (again,) = await F._claim_due(settings)
    assert again["id"] == row["id"] and F.FIBER_LAPSES_KEY not in again["state_json"]


async def test_a_worker_that_stops_mid_step_leaves_no_step_running(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same, through the loop that does the cancelling: when it returns, the step has ended
    and the run is another worker's to pick up."""
    settings = make_settings(host="127.0.0.1", fiber_poll_seconds=0.01)
    await F.create_fiber(
        settings, "t", status="pending", state={"steps": [{"op": "invoke"}], "cursor": 0, "stash": {}}
    )
    step = _Step()
    monkeypatch.setattr(F, "_run_fiber_step", step)
    monkeypatch.setattr(F, "FIBER_LOOP_DRAIN_S", 0.01)
    stop = asyncio.Event()
    loop = asyncio.create_task(F.run_fiber_loop(settings, stop))
    await asyncio.wait_for(step.started.wait(), timeout=5)

    stop.set()
    await asyncio.wait_for(loop, timeout=5)

    assert step.cancelled, "the worker stopped with a step still running"
    (stored,) = F._memory_fibers.values()
    assert (stored["status"], stored["lease_until"]) == ("running", None), "its claim was not given back"


async def test_a_cancelled_claim_that_cannot_be_given_back_is_left_to_lapse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The store is down as the worker stops. The cancel still goes through as a cancel, and the
    lease is what it always was for a worker that vanished: it runs out."""
    settings = _settings()
    row = await _claimed(settings)
    held = (row["lease_owner"], row["lease_until"])
    step = _Step()
    monkeypatch.setattr(F, "_run_fiber_step", step)

    async def store_down(*_a: Any, **_k: Any) -> None:
        raise ConnectionError("store down")

    monkeypatch.setattr(F, "_release_fiber", store_down)
    advancing = asyncio.create_task(F._advance_claimed(settings, row))
    await asyncio.wait_for(step.started.wait(), timeout=5)
    advancing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(advancing, timeout=5)

    assert step.cancelled
    stored = _stored(row)
    assert (stored["lease_owner"], stored["lease_until"]) == held


# --- a renewal that landed, and whose answer never came back -------------------------------------


async def test_a_renewal_whose_answer_was_lost_does_not_stop_the_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The write committed and the client raised. The holder went on knowing only the lease it
    had before, so its next renewal matched no row, read that as the fiber taken, and cancelled a
    healthy step mid-turn -- leaving the store naming it owner of a fresh lease nobody renewed.
    A claim is known by its name now, which a renewal does not change."""
    settings = _settings()
    row = await _claimed(settings)
    step = _Step()
    monkeypatch.setattr(F, "_run_fiber_step", step)
    monkeypatch.setattr(F, "FIBER_LEASE_RENEW_MS", 10)
    real = F._renew_lease
    calls = 0
    renewed_after = asyncio.Event()

    async def lands_unheard_once(settings: Any, row: dict[str, Any]) -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            # In the store, and never in the holder's hands.
            assert await real(settings, dict(row))
            raise ConnectionError("the answer was lost")
        landed = await real(settings, row)
        if landed:
            renewed_after.set()
        return landed

    monkeypatch.setattr(F, "_renew_lease", lands_unheard_once)
    # Renewals a millisecond apart would all ask for the same lease; the clock moves between them.
    clock = {"now": F.now_ms()}

    def tick() -> int:
        clock["now"] += 1000
        return clock["now"]

    monkeypatch.setattr(F, "now_ms", tick)

    advancing = asyncio.create_task(F._advance_claimed(settings, row))
    await asyncio.wait_for(step.started.wait(), timeout=5)
    await asyncio.wait(
        {advancing, asyncio.create_task(renewed_after.wait())}, timeout=5, return_when=asyncio.FIRST_COMPLETED
    )
    cancelled = step.cancelled
    step.release.set()
    await asyncio.wait_for(advancing, timeout=5)

    assert not cancelled, "a claim the store still held was read as taken"
    assert renewed_after.is_set(), "the holder never renewed its own lease again"


# --- the heartbeat counts from the claim, not from when the step began ---------------------------


async def test_a_claim_that_was_slow_to_start_gives_up_on_the_lease_it_was_written_with(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Between the claim and the step the claim may read a thread log, for every row claimed
    before this one. The heartbeat dated the lease from its own start, so with the store then
    failing it went on retrying well past the moment another worker could take the fiber."""
    settings = _settings()
    clock = {"now": F.now_ms()}
    monkeypatch.setattr(F, "now_ms", lambda: clock["now"])
    row = await _claimed(settings)
    clock["now"] = row["lease_until"] - 1  # the step starts with a millisecond of lease left
    step = _Step()
    monkeypatch.setattr(F, "_run_fiber_step", step)
    monkeypatch.setattr(F, "FIBER_LEASE_RENEW_MS", 10)
    calls = 0

    async def down(*_a: Any, **_k: Any) -> bool:
        nonlocal calls
        calls += 1
        raise ConnectionError("store down")

    monkeypatch.setattr(F, "_renew_lease", down)
    advancing = asyncio.create_task(F._advance_claimed(settings, row))
    try:
        await asyncio.wait_for(asyncio.shield(advancing), timeout=2)
    except TimeoutError:
        pytest.fail(f"the step ran on under a lease that had lapsed; {calls} renewals were tried")
    finally:
        advancing.cancel()

    assert (step.cancelled, calls) == (True, 1)


# --- the worker's own shutdown -------------------------------------------------------------------


async def test_the_workers_shutdown_stops_a_step_and_gives_its_fiber_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole path, as Taskiq runs it: the shutdown handler under the timeout the worker asks
    for. The timeout was Taskiq's default five seconds against a thirty-second drain, so the
    handler was cancelled inside the drain -- no fiber was ever cancelled, none gave its claim
    back, and the flush behind the drain did not run. Scaled down here, in the same proportions.
    """
    import felix_worker.tasks as tasks
    from felix.usage import store as usage_store
    from felix_worker.main import worker_args

    scale = 0.02
    settings = make_settings(host="127.0.0.1", fiber_poll_seconds=0.01)
    monkeypatch.setattr(tasks, "_settings", settings)
    monkeypatch.setattr(F, "FIBER_LOOP_DRAIN_S", F.FIBER_LOOP_DRAIN_S * scale)
    monkeypatch.setattr(F, "FIBER_LOOP_STOP_S", F.FIBER_LOOP_STOP_S * scale)
    await F.create_fiber(
        settings, "t", status="pending", state={"steps": [{"op": "invoke"}], "cursor": 0, "stash": {}}
    )
    step = _Step()
    monkeypatch.setattr(F, "_run_fiber_step", step)
    stop = asyncio.Event()
    monkeypatch.setattr(tasks, "_fiber_loop_stop", stop)
    monkeypatch.setattr(tasks, "_fiber_loop", asyncio.create_task(F.run_fiber_loop(settings, stop)))
    await asyncio.wait_for(step.started.wait(), timeout=5)
    usage_store.record_tokens(settings, tenant_id="t", manifest_id="m", model_id="m", tokens_input=1)

    # `taskiq.cli.worker.run.shutdown_broker`: the handler under `shutdown_timeout`, and a
    # timeout swallowed.
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(
            tasks._on_worker_shutdown(None), timeout=worker_args().shutdown_timeout * scale
        )

    assert step.cancelled, "the worker exited with a step still running"
    (stored,) = F._memory_fibers.values()
    assert (stored["status"], stored["lease_until"]) == ("running", None), "its claim was not given back"
    assert usage_store.pending_count() == 0, "the flush behind the drain never ran"


async def test_a_stopping_worker_drains_what_a_sweep_is_running_and_claims_nothing_new(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the poll off the cron sweep is the only thing running fibers, and nothing drained
    its: Taskiq waited on the sweep without limit, or cut it off with its fiber under a lease."""
    settings = make_settings(host="127.0.0.1", fiber_poll_seconds=0, fiber_concurrency=1)
    for _ in range(2):
        await F.create_fiber(
            settings, "t", status="pending", state={"steps": [{"op": "invoke"}], "cursor": 0, "stash": {}}
        )
    step = _Step()
    monkeypatch.setattr(F, "_run_fiber_step", step)
    monkeypatch.setattr(F, "FIBER_LOOP_DRAIN_S", 0.05)
    sweep = asyncio.create_task(F.resume_due_fibers(settings))
    await asyncio.wait_for(step.started.wait(), timeout=5)

    await asyncio.wait_for(F.drain_fibers(), timeout=5)
    await asyncio.wait_for(sweep, timeout=5)

    assert step.cancelled
    first, second = F._memory_fibers.values()
    assert (first["status"], first["lease_until"]) == ("running", None), "the sweep's fiber kept its lease"
    assert (second["status"], second.get("lease_until")) == ("pending", None), (
        "claimed by a worker on its way out"
    )


async def test_a_step_that_ignores_its_cancel_does_not_hold_the_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tool that swallows cancellation would otherwise hold shutdown until the container is
    killed, taking the flush with it. It is left behind, under its lease."""
    settings = make_settings(host="127.0.0.1", fiber_poll_seconds=0)
    await F.create_fiber(
        settings, "t", status="pending", state={"steps": [{"op": "invoke"}], "cursor": 0, "stash": {}}
    )
    started, let_go = asyncio.Event(), asyncio.Event()

    async def deaf(settings: Any, row: dict[str, Any], **_kw: Any) -> dict[str, Any]:
        started.set()
        while not let_go.is_set():
            with contextlib.suppress(asyncio.CancelledError):
                await let_go.wait()
        raise asyncio.CancelledError

    monkeypatch.setattr(F, "_run_fiber_step", deaf)
    monkeypatch.setattr(F, "FIBER_LOOP_DRAIN_S", 0.02)
    monkeypatch.setattr(F, "FIBER_LOOP_STOP_S", 0.05)
    sweep = asyncio.create_task(F.resume_due_fibers(settings))
    await asyncio.wait_for(started.wait(), timeout=5)
    try:
        await asyncio.wait_for(F.drain_fibers(), timeout=2)
        (stored,) = F._memory_fibers.values()
        assert stored["lease_until"] is not None, "a claim was given up under a step still running"
    finally:
        let_go.set()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(sweep, timeout=5)
