"""A failed `tool_call` audit row says which class of failure it was.

The tool runner reads the call's `ToolErrorCode` to set the row's status, and used to drop it:
the row said `error` and nothing else, so the audit log could answer "did it fail" and never
"why". It now records the code — and only the code, since the message is the tool's own text and
an audit row outlives its thread.

Driven through the real `ToolRunner` with only the audit sink replaced, patched where the runner
looks it up, as `test_audit_deny_control.py` does.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.config import get_settings
from felix.context import AuthContext, RequestContext, async_run_with_context
from felix.patterns import tool_runner as runner_mod
from felix.patterns.tool_runner import ToolRunner
from felix.patterns.types import ToolCall
from felix.tools.errors import ToolErrorCode, tool_error_output
from felix.tools.types import Tool, ToolInput, ToolInvocationCtx, ToolOutput


class _Returns:
    transport = "local"

    def __init__(self, output: ToolOutput) -> None:
        self._output = output

    async def execute(self, args: ToolInput, ctx: ToolInvocationCtx | None = None) -> ToolOutput:
        return self._output


async def _audit_of(
    monkeypatch: pytest.MonkeyPatch, output: ToolOutput
) -> list[tuple[str, str, dict[str, Any]]]:
    recorded: list[tuple[str, str, dict[str, Any]]] = []
    monkeypatch.setattr(
        runner_mod,
        "emit_agent_audit",
        lambda kind, **kw: recorded.append((kind, str(kw.get("status")), dict(kw.get("payload") or {}))),
    )
    ctx = RequestContext(
        settings=get_settings(),
        auth=AuthContext(principal_sub="p", tenant_id="t"),
        manifest_id="m",
        thread_id="th",
    )
    tool = Tool(name="t", description="d", args_schema=None, executor=_Returns(output))
    async with async_run_with_context(ctx):
        await ToolRunner(tool_map={"t": tool}, manifest_id="m").run_batch(
            [ToolCall(id="1", name="t", args={})], thread_id="th", tenant_id="t"
        )
    return recorded


async def test_a_failed_call_records_its_error_code(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "token=sk-live-do-not-audit"
    recorded = await _audit_of(
        monkeypatch, tool_error_output(ToolErrorCode.PERMISSION_DENIED, f"refused: {secret}")
    )
    assert [(k, s) for k, s, _ in recorded] == [("tool_call", "error")]
    payload = recorded[0][2]
    assert payload["error_code"] == "permission_denied", payload
    # The message is the tool's text; it stays on the tool card, not in the audit log.
    assert secret not in repr(payload)


async def test_a_successful_call_records_no_error_code(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded = await _audit_of(monkeypatch, "ran")
    assert [(k, s) for k, s, _ in recorded] == [("tool_call", "ok")]
    assert "error_code" not in recorded[0][2]
