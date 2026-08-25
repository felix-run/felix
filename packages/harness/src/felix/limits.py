"""Run-budget enforcement for `spec.limits`.

`max_wall_clock_seconds`, `max_input_tokens`, and `max_output_tokens` were declared in
the manifest schema, range-bounded, and documented — and appeared nowhere else in the
codebase. Worse, `any_limit()` counted them toward `_has_boundary_control`, so a manifest
satisfied the SOC 2 compile check `"require non-empty policies, approvals, or limits"`
with a limit that had no runtime behaviour at all. A validator that attests to a control
which does not exist is worse than no validator.

This module is the single place those budgets are evaluated, so the tool wrapper and the
agent loop cannot drift apart on what "over budget" means.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from felix.context import LimitState


@dataclass(frozen=True)
class BudgetVerdict:
    """The outcome of a budget check. ``reason`` is empty when within budget."""

    exceeded: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.exceeded


_OK = BudgetVerdict(False)


def check_budgets(limits: Any, state: LimitState, *, now: int | None = None) -> BudgetVerdict:
    """Evaluate every declared run budget against accumulated state.

    Checked before each tool call and at the top of each agent turn, so a run cannot
    exceed a budget by more than one step.
    """
    if limits is None:
        return _OK

    wall = getattr(limits, "max_wall_clock_seconds", None)
    if wall is not None:
        elapsed_ms = state.elapsed_ms(now)
        if elapsed_ms >= float(wall) * 1000.0:
            return BudgetVerdict(True, f"max_wall_clock_seconds ({wall}) exceeded after {elapsed_ms}ms")

    max_in = getattr(limits, "max_input_tokens", None)
    if max_in is not None and state.tokens_input >= int(max_in):
        return BudgetVerdict(True, f"max_input_tokens ({max_in}) exceeded at {state.tokens_input}")

    max_out = getattr(limits, "max_output_tokens", None)
    if max_out is not None and state.tokens_output >= int(max_out):
        return BudgetVerdict(True, f"max_output_tokens ({max_out}) exceeded at {state.tokens_output}")

    max_cost = getattr(limits, "max_cost_usd", None)
    if max_cost is not None and state.cost_usd >= float(max_cost):
        return BudgetVerdict(True, f"max_cost_usd ({max_cost}) exceeded at {state.cost_usd:.4f}")

    return _OK


def trip(state: LimitState, reason: str) -> None:
    """Mark the run aborted so every later wrapper and the agent loop stop too."""
    state.aborted = True
    if not state.abort_reason:
        state.abort_reason = reason


@dataclass(frozen=True)
class _EffectiveLimits:
    """A manifest's limits with every unset field filled from ABSOLUTE_LIMITS.

    ``ABSOLUTE_LIMITS`` is documented as the maximum a manifest may declare, but nothing
    applied it when a manifest declared nothing — so the default posture was no cap at
    all on tool calls, wall clock, tokens, or spend. Filling the gaps makes the
    documented absolute the effective ceiling for every run.
    """

    max_tool_calls: int | None
    max_peer_hops: int | None
    max_wall_clock_seconds: float | None
    max_input_tokens: int | None
    max_output_tokens: int | None
    max_cost_usd: float | None


def effective_limits(limits: Any) -> _EffectiveLimits:
    from felix.manifests.schema import ABSOLUTE_LIMITS

    def pick(name: str) -> Any:
        declared = getattr(limits, name, None) if limits is not None else None
        return declared if declared is not None else ABSOLUTE_LIMITS.get(name)

    return _EffectiveLimits(
        max_tool_calls=pick("max_tool_calls"),
        max_peer_hops=pick("max_peer_hops"),
        max_wall_clock_seconds=pick("max_wall_clock_seconds"),
        max_input_tokens=pick("max_input_tokens"),
        max_output_tokens=pick("max_output_tokens"),
        max_cost_usd=pick("max_cost_usd"),
    )


__all__ = ["BudgetVerdict", "check_budgets", "effective_limits", "trip"]
