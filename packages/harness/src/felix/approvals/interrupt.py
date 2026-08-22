"""Pause a tool call until an approval decision arrives."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from felix.waiters import signal as waiter_signal
from felix.waiters import wait as waiter_wait

DEFAULT_TIMEOUT_SECONDS = 300.0


@dataclass(slots=True)
class ApprovalDecision:
    decision: Literal["approved", "denied"]
    edited_args: dict[str, Any] | None = None
    note: str = ""


def _name(approval_id: str) -> str:
    return f"approval:{approval_id}"


async def wait_for_decision(
    approval_id: str,
    *,
    timeout: float | None = None,
) -> ApprovalDecision:
    limit = DEFAULT_TIMEOUT_SECONDS if timeout is None else float(timeout)
    payload = await waiter_wait(_name(approval_id), timeout=limit)
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
