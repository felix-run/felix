"""`spec.limits` budgets must actually bound a run.

`max_wall_clock_seconds`, `max_input_tokens`, and `max_output_tokens` were declared in
the schema, range-bounded, and documented — and appeared nowhere else in the codebase.
`LimitState.started_at_ms` was never set or read. Meanwhile `any_limit()` counted them
toward `_has_boundary_control`, so a manifest satisfied the SOC 2 check
"require non-empty policies, approvals, or limits" with a limit that did nothing at all.
"""

from __future__ import annotations

import time

import pytest
from felix.context import LimitState
from felix.limits import check_budgets, effective_limits, trip
from felix.manifests.schema import ABSOLUTE_LIMITS, Limits


def _state(**kw: object) -> LimitState:
    ls = LimitState()
    for k, v in kw.items():
        setattr(ls, k, v)
    return ls


# --- each budget is real --------------------------------------------------------


def test_wall_clock_is_enforced() -> None:
    ls = _state(started_at_ms=int(time.time() * 1000) - 5_000)
    v = check_budgets(Limits(max_wall_clock_seconds=1), ls)
    assert v.exceeded and "max_wall_clock_seconds" in v.reason


def test_wall_clock_within_budget_passes() -> None:
    ls = _state(started_at_ms=int(time.time() * 1000))
    assert not check_budgets(Limits(max_wall_clock_seconds=60), ls).exceeded


def test_input_tokens_enforced() -> None:
    v = check_budgets(Limits(max_input_tokens=100), _state(tokens_input=100))
    assert v.exceeded and "max_input_tokens" in v.reason


def test_output_tokens_enforced() -> None:
    v = check_budgets(Limits(max_output_tokens=50), _state(tokens_output=99))
    assert v.exceeded and "max_output_tokens" in v.reason


def test_cost_ceiling_enforced() -> None:
    v = check_budgets(Limits(max_cost_usd=1.0), _state(cost_usd=1.25))
    assert v.exceeded and "max_cost_usd" in v.reason


def test_undeclared_budgets_do_not_trip() -> None:
    assert not check_budgets(Limits(), _state(tokens_input=10**6, cost_usd=999.0)).exceeded


def test_started_at_is_populated_by_default() -> None:
    """It defaulted to 0, so elapsed time was measured from 1970."""
    assert LimitState().started_at_ms > 0
    assert LimitState().elapsed_ms() < 1000


def test_trip_records_the_first_reason() -> None:
    ls = LimitState()
    trip(ls, "first")
    trip(ls, "second")
    assert ls.aborted is True
    assert ls.abort_reason == "first"


# --- the default posture is bounded ---------------------------------------------


def test_absolute_limits_fill_undeclared_fields() -> None:
    """A manifest that declares nothing previously got no cap of any kind."""
    eff = effective_limits(Limits())
    assert eff.max_tool_calls == ABSOLUTE_LIMITS["max_tool_calls"]
    assert eff.max_wall_clock_seconds == ABSOLUTE_LIMITS["max_wall_clock_seconds"]
    assert eff.max_input_tokens == ABSOLUTE_LIMITS["max_input_tokens"]
    assert eff.max_cost_usd == ABSOLUTE_LIMITS["max_cost_usd"]


def test_declared_limits_win_over_absolutes() -> None:
    eff = effective_limits(Limits(max_tool_calls=3, max_cost_usd=0.5))
    assert eff.max_tool_calls == 3
    assert eff.max_cost_usd == 0.5
    assert eff.max_peer_hops == ABSOLUTE_LIMITS["max_peer_hops"]


def test_effective_limits_handles_none() -> None:
    assert effective_limits(None).max_tool_calls == ABSOLUTE_LIMITS["max_tool_calls"]


# --- the tool wrapper -----------------------------------------------------------


@pytest.mark.asyncio
async def test_wrapper_denies_when_over_budget() -> None:
    from dataclasses import dataclass

    from felix.config import Settings
    from felix.context import AuthContext, RequestContext, async_run_with_context
    from felix.manifests.builder import apply_limits
    from felix.tools.types import Tool, ToolInput, ToolInvocationCtx, ToolOutput, is_wrapper_deny

    @dataclass
    class _Exec:
        @property
        def transport(self) -> str:
            return "local"

        async def execute(self, args: ToolInput, ctx: ToolInvocationCtx | None = None) -> ToolOutput:
            return "ran"

    tools = apply_limits(
        [Tool(name="t", description="d", args_schema={}, executor=_Exec())],
        Limits(max_cost_usd=1.0),
        "m",
    )
    s = Settings(database_url="memory://b", object_store="memory", allow_insecure=True, auth_mode="none")
    ctx = RequestContext(settings=s, auth=AuthContext())
    ctx.limit_state.cost_usd = 5.0
    async with async_run_with_context(ctx):
        out = await tools[0].executor.execute({}, None)
    assert is_wrapper_deny(out)
    assert ctx.limit_state.aborted is True


@pytest.mark.asyncio
async def test_wrapper_fails_closed_without_a_request_context() -> None:
    """It used to skip every check entirely when there was no context."""
    from dataclasses import dataclass

    from felix.manifests.builder import apply_limits
    from felix.tools.types import Tool, ToolInput, ToolInvocationCtx, ToolOutput, is_wrapper_deny

    @dataclass
    class _Exec:
        @property
        def transport(self) -> str:
            return "local"

        async def execute(self, args: ToolInput, ctx: ToolInvocationCtx | None = None) -> ToolOutput:
            return "ran"

    tools = apply_limits([Tool(name="t", description="d", args_schema={}, executor=_Exec())], Limits(), "m")
    out = await tools[0].executor.execute({}, None)
    assert is_wrapper_deny(out)
