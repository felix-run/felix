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
from typing import TYPE_CHECKING

from felix.context import LimitState

if TYPE_CHECKING:
    from felix.manifests.schema import Limits


@dataclass(frozen=True)
class BudgetVerdict:
    """The outcome of a budget check. ``reason`` is empty when within budget."""

    exceeded: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.exceeded


_OK = BudgetVerdict(False)


def check_budgets(
    limits: Limits | EffectiveLimits | None, state: LimitState, *, now: int | None = None
) -> BudgetVerdict:
    """Evaluate every declared run budget against accumulated state.

    Checked before each tool call and at the top of each agent turn, so a run cannot
    exceed a budget by more than one step.

    Both shapes are accepted because both reach here: the agent loop and the tool wrapper
    are handed `EffectiveLimits`, while a manifest's raw `Limits` is what tests and
    direct callers have. They declare the same budget fields, and reading them as
    attributes rather than through `getattr` defaults is what makes that claim checkable.
    """
    if limits is None:
        return _OK

    wall = limits.max_wall_clock_seconds
    if wall is not None:
        elapsed_ms = state.elapsed_ms(now)
        if elapsed_ms >= float(wall) * 1000.0:
            return BudgetVerdict(True, f"max_wall_clock_seconds ({wall}) exceeded after {elapsed_ms}ms")

    max_in = limits.max_input_tokens
    if max_in is not None and state.tokens_input >= int(max_in):
        return BudgetVerdict(True, f"max_input_tokens ({max_in}) exceeded at {state.tokens_input}")

    max_out = limits.max_output_tokens
    if max_out is not None and state.tokens_output >= int(max_out):
        return BudgetVerdict(True, f"max_output_tokens ({max_out}) exceeded at {state.tokens_output}")

    max_cost = limits.max_cost_usd
    if max_cost is not None and state.cost_usd >= float(max_cost):
        return BudgetVerdict(True, f"max_cost_usd ({max_cost}) exceeded at {state.cost_usd:.4f}")

    return _OK


def trip(state: LimitState, reason: str) -> None:
    """Mark the run aborted so every later wrapper and the agent loop stop too."""
    state.aborted = True
    if not state.abort_reason:
        state.abort_reason = reason


@dataclass(frozen=True)
class EffectiveLimits:
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


def effective_limits(limits: Limits | None) -> EffectiveLimits:
    """A manifest's declared limits, with ABSOLUTE_LIMITS filling every unset field."""
    from felix.manifests.schema import ABSOLUTE_LIMITS

    # Indexed, not `.get()`: a missing key would silently become `None`, which means
    # "no cap at all" — the exact posture this function exists to prevent.
    def cap_int(declared: int | None, name: str) -> int:
        return declared if declared is not None else int(ABSOLUTE_LIMITS[name])

    def cap_float(declared: float | None, name: str) -> float:
        return declared if declared is not None else float(ABSOLUTE_LIMITS[name])

    return EffectiveLimits(
        max_tool_calls=cap_int(limits.max_tool_calls if limits else None, "max_tool_calls"),
        max_peer_hops=cap_int(limits.max_peer_hops if limits else None, "max_peer_hops"),
        max_wall_clock_seconds=cap_float(
            limits.max_wall_clock_seconds if limits else None, "max_wall_clock_seconds"
        ),
        max_input_tokens=cap_int(limits.max_input_tokens if limits else None, "max_input_tokens"),
        max_output_tokens=cap_int(limits.max_output_tokens if limits else None, "max_output_tokens"),
        max_cost_usd=cap_float(limits.max_cost_usd if limits else None, "max_cost_usd"),
    )


__all__ = ["BudgetVerdict", "EffectiveLimits", "check_budgets", "effective_limits", "trip"]
