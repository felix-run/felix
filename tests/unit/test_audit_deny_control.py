"""A `policy_deny` audit row names the control that refused the call.

Every governance wrapper stamps its source on the deny it returns (`deny_output`), and the
loop threw that away when it wrote the audit row — so "show me every call blocked by policy X"
was unanswerable from the audit log, and the roadmap said so. This pins the read side.

The wrapper under test is the real `apply_policies`, driven through the real `ToolRunner`, with
only the audit sink replaced — patched where the loop looks it up, not where it is defined.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.config import get_settings
from felix.context import AuthContext, RequestContext, async_run_with_context
from felix.manifests.builder import apply_policies
from felix.manifests.schema import Policy
from felix.patterns import tool_runner as runner_mod
from felix.patterns.tool_runner import ToolRunner
from felix.patterns.types import ToolCall
from felix.tools.types import Tool, ToolInput, ToolInvocationCtx, ToolOutput, deny_output, deny_source


class _Echo:
    transport = "local"

    async def execute(self, args: ToolInput, ctx: ToolInvocationCtx | None = None) -> ToolOutput:
        return "ran"


def _policy_gated_tool() -> Tool:
    tool = Tool(name="t", description="d", args_schema=None, executor=_Echo())
    policy = Policy(id="needs-scope", tools=["t"], required_scopes=["tools:t"])
    return apply_policies([tool], [policy], manifest_id="m")[0]


async def _run_and_capture(
    monkeypatch: pytest.MonkeyPatch, *, scopes: frozenset[str]
) -> list[tuple[str, dict[str, Any]]]:
    recorded: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        runner_mod,
        "emit_agent_audit",
        lambda kind, **kw: recorded.append((kind, dict(kw.get("payload") or {}))),
    )
    ctx = RequestContext(
        settings=get_settings(),
        auth=AuthContext(principal_sub="p", tenant_id="t", scopes=scopes),
        manifest_id="m",
        thread_id="th",
    )
    async with async_run_with_context(ctx):
        await ToolRunner(tool_map={"t": _policy_gated_tool()}, manifest_id="m").run_batch(
            [ToolCall(id="1", name="t", args={})], thread_id="th", tenant_id="t"
        )
    return recorded


@pytest.mark.asyncio
async def test_a_policy_denial_names_its_control(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded = await _run_and_capture(monkeypatch, scopes=frozenset())
    assert [k for k, _ in recorded] == ["policy_deny"]
    payload = recorded[0][1]
    assert payload["control"] == "policy", payload
    assert payload["tool"] == "t" and payload["tool_call_id"] == "1"


@pytest.mark.asyncio
async def test_a_permitted_call_carries_no_control(monkeypatch: pytest.MonkeyPatch) -> None:
    """`control` is a fact about a denial. A `tool_call` row must not grow a null key."""
    recorded = await _run_and_capture(monkeypatch, scopes=frozenset({"tools:t"}))
    assert [k for k, _ in recorded] == ["tool_call"]
    assert "control" not in recorded[0][1]


def test_deny_source_reads_every_shape_a_deny_can_take() -> None:
    assert deny_source(deny_output("no", "approvals")) == "approvals"
    assert deny_source(deny_output("no", "screening")) == "screening"
    assert deny_source("a plain string result") is None
    # A dict that merely *says* source without the private marker is not a wrapper deny.
    assert deny_source({"content": "x", "metadata": {"source": "policy"}}) is None
