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
    row = await _claimed(settings)
    step = _Step()
    monkeypatch.setattr(F, "_run_fiber_step", step)
    monkeypatch.setattr(F, "FIBER_LEASE_MS", 300)
    monkeypatch.setattr(F, "FIBER_LEASE_RENEW_MS", 100)
    started = F.now_ms()

    async def down(*_a: Any, **_k: Any) -> bool:
        raise ConnectionError("store down")

    monkeypatch.setattr(F, "_renew_lease", down)

    await asyncio.wait_for(F._advance_claimed(settings, row), timeout=5)

    assert step.cancelled
    assert F.now_ms() - started < 300, "the step outlived the lease it could not renew"


# --- a claim cut short by the worker stopping ----------------------------------------------------


async def test_a_cancelled_claim_stops_its_step_and_keeps_its_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    """The worker's drain ran out and the fiber's task was cancelled.

    The cancel landed on the wait *for* the step, which cancels nothing it waits on: the step
    ran on with no task behind it, and the claim was released on the way out -- so another worker
    took the fiber at once and ran the same turn beside it. Now the step is stopped and the lease
    is left to lapse, which is what the comment on `FIBER_LOOP_DRAIN_S` always said happened.
    """
    settings = _settings()
    row = await _claimed(settings)
    held = (row["lease_owner"], row["lease_until"])
    step = _Step()
    monkeypatch.setattr(F, "_run_fiber_step", step)

    advancing = asyncio.create_task(F._advance_claimed(settings, row))
    await asyncio.wait_for(step.started.wait(), timeout=5)
    advancing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(advancing, timeout=5)

    assert step.cancelled, "the step outlived the task that was running it"
    stored = _stored(row)
    assert (stored["status"], stored["lease_owner"], stored["lease_until"]) == ("running", *held), (
        "the claim was released under a step that had not finished"
    )
    assert await F._claim_due(settings) == [], "another worker could take the fiber at once"


async def test_a_worker_that_stops_mid_step_leaves_no_step_running(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same, through the loop that does the cancelling: when it returns, the step has ended."""
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
    assert stored["status"] == "running" and stored["lease_until"] is not None, "its claim was released"
