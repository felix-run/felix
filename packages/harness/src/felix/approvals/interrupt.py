"""Pause a tool call until an approval decision arrives."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from felix.waiters import signal as waiter_signal
from felix.waiters import wait as waiter_wait
from felix.waiters import waiter_name

logger = logging.getLogger("felix.approvals.interrupt")

DEFAULT_TIMEOUT_SECONDS = 300.0
#: How often a wait with a `check` asks it. The signal is the fast path; this is the floor.
CHECK_SECONDS = 5.0


@dataclass(slots=True)
class ApprovalDecision:
    decision: Literal["approved", "denied"]
    edited_args: dict[str, Any] | None = None
    note: str = ""


def _name(approval_id: str) -> str:
    # A `uuid4().hex` from the approvals store, so single-part and unambiguous. Same reason
    # as `ui` for going through the shared join: the escaping should not be a thing to
    # remember when a second part shows up.
    return waiter_name("approval", approval_id)


async def wait_for_decision(
    approval_id: str,
    *,
    timeout: float | None = None,
    check: Callable[[], Awaitable[ApprovalDecision | None]] | None = None,
) -> ApprovalDecision:
    """Block until the approval is decided, or `timeout` passes (denied, note `timeout`).

    `check`, when given, is asked every `CHECK_SECONDS` while the signal has not arrived, and
    a decision it returns ends the wait. It is how a wait hears what the signal cannot carry
    (felix-run/felix#532): the approval *row*, which `/approvals/{id}/decide` writes before it
    signals, so a signal lost between processes costs seconds rather than the decision; and a
    Stop on the thread, which used to leave a gated call waiting out its whole deadline.
    """
    limit = DEFAULT_TIMEOUT_SECONDS if timeout is None else float(timeout)
    if check is None:
        payload = await waiter_wait(_name(approval_id), timeout=limit)
    else:
        decided, payload = await _wait_or_check(approval_id, limit, check)
        if decided is not None:
            return decided
    if payload is None:
        return ApprovalDecision(decision="denied", note="timeout")
    decision = payload.get("decision")
    if decision not in ("approved", "denied"):
        return ApprovalDecision(decision="denied", note="invalid")
    edited = payload.get("edited_args")
    return ApprovalDecision(
        decision=decision,
        edited_args=dict(edited) if isinstance(edited, dict) else None,
        note=str(payload.get("note") or ""),
    )


async def _wait_or_check(
    approval_id: str,
    limit: float,
    check: Callable[[], Awaitable[ApprovalDecision | None]],
) -> tuple[ApprovalDecision | None, dict[str, Any] | None]:
    waiter = asyncio.ensure_future(waiter_wait(_name(approval_id), timeout=limit))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + limit
    try:
        while True:
            remaining = deadline - loop.time()
            done, _ = await asyncio.wait({waiter}, timeout=max(0.0, min(CHECK_SECONDS, remaining)))
            if done:
                return None, waiter.result()
            try:
                decided = await check()
            except Exception:
                logger.debug("approval wait check failed for %s", approval_id, exc_info=True)
                decided = None
            if decided is not None:
                return decided, None
    finally:
        if not waiter.done():
            waiter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await waiter


async def signal_decision(
    approval_id: str,
    decision: Literal["approved", "denied"],
    *,
    edited_args: dict[str, Any] | None = None,
    note: str = "",
) -> bool:
    return await waiter_signal(
        _name(approval_id),
        {"decision": decision, "edited_args": edited_args, "note": note},
    )


# Kept for API compatibility with earlier interrupt helpers.
async def prepare_waiter(approval_id: str) -> None:
    _ = approval_id


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "ApprovalDecision",
    "prepare_waiter",
    "signal_decision",
    "wait_for_decision",
]
