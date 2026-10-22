"""An MCP client's `tools/call` runs through the agent loop's own dispatch (`ToolRunner`).

It called `tool.executor.execute` directly, so the governed executor ran and nothing around it
did: no plugin `before_tool` / `after_tool` hook, no manifest `pre_tool_use` / `post_tool_use`, no
`tool_call` audit row. An MCP client was the one caller whose tool calls no hook could refuse and
no audit reader could see.
"""

from __future__ import annotations

from typing import Any

from felix.context import AuthContext
from felix_api.composition import compose

from tests.support.factories import make_settings
from tests.support.mcp_server import mcp_rpc


async def _calc(settings: Any, expression: str = "6*7") -> dict[str, Any]:
    out = await mcp_rpc(
        settings=settings,
        tools=compose(settings),
        method="tools/call",
        params={"name": "calculator", "arguments": {"expression": expression}},
        rpc_id=1,
        auth=AuthContext(),
    )
    return out["result"]


async def test_a_plugin_before_tool_hook_refuses_an_mcp_call() -> None:
    from felix.hooks import get_agent_hooks

    seen: list[dict[str, Any]] = []

    async def refuse(call: dict[str, Any], _ctx: dict[str, Any]) -> dict[str, Any]:
        seen.append(call)
        return {"block": True, "reason": "not over MCP"}

    get_agent_hooks().register_before_tool(refuse)
    result = await _calc(make_settings())
    assert result["isError"] is True
    assert "42" not in result["content"][0]["text"], "the tool never ran"
    assert seen and seen[0]["name"] == "calculator" and seen[0]["args"] == {"expression": "6*7"}


async def test_an_after_tool_hook_rewrites_what_the_client_is_told() -> None:
    from felix.hooks import get_agent_hooks

    async def redact(
        _call: dict[str, Any], _result: Any, _is_error: bool, _ctx: dict[str, Any]
    ) -> dict[str, Any]:
        return {"content": "[redacted]"}

    get_agent_hooks().register_after_tool(redact)
    result = await _calc(make_settings())
    assert result["content"] == [{"type": "text", "text": "[redacted]"}]
    assert result["isError"] is False


async def test_an_mcp_call_writes_the_tool_call_audit_row() -> None:
    from felix.audit import store as audit_store

    settings = make_settings()
    result = await _calc(settings)
    assert "42" in result["content"][0]["text"]
    await audit_store.flush_pending(settings)
    rows, _cursor = await audit_store.list_events(settings, "default", event_type="tool_call")
    assert [(r["status"], r["payload_json"].get("tool")) for r in rows] == [("ok", "calculator")]
