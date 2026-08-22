"""MCP JSON-RPC surface — tools/list + tools/call over Felix ToolProvider."""

from __future__ import annotations

from typing import Any

from felix import __version__
from felix.config import Settings
from felix.tools.provider import ToolProvider
from felix.tools.types import ToolInvocationCtx, is_wrapper_deny, tool_output_content


def _tool_descriptor(tool: Any) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": {}}
    if getattr(tool, "raw_input_schema", None):
        schema = dict(tool.raw_input_schema)
    elif getattr(tool, "args_schema", None) is not None:
        args = tool.args_schema
        if isinstance(args, dict):
            schema = args
        elif hasattr(args, "model_json_schema"):
            schema = args.model_json_schema()
    return {
        "name": tool.name,
        "description": tool.description,
        "inputSchema": schema,
    }


async def handle_rpc(
    *,
    settings: Settings,
    tools: ToolProvider,
    method: str,
    params: dict[str, Any],
    rpc_id: str | int | None,
) -> dict[str, Any]:
    _ = settings
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "result": {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "felix", "version": __version__},
            },
        }
    if method == "notifications/initialized":
        return {"jsonrpc": "2.0", "id": rpc_id, "result": {}}
    if method == "tools/list":
        names = tools.list()
        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "result": {"tools": [_tool_descriptor(tools.get(n)) for n in names]},
        }
    if method == "tools/call":
        name = str(params.get("name") or "")
        args = params.get("arguments") or {}
        if not name or not tools.has(name):
            return {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "error": {"code": -32602, "message": f"Unknown tool: {name}"},
            }
        tool = tools.get(name)
        try:
            out = await tool.executor.execute(
                dict(args) if isinstance(args, dict) else {},
                ToolInvocationCtx(manifest_id="mcp"),
            )
            text = tool_output_content(out)
            denied = is_wrapper_deny(out)
            return {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "result": {
                    "content": [{"type": "text", "text": text}],
                    "isError": denied,
                },
            }
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "result": {
                    "content": [{"type": "text", "text": f"error: {exc}"}],
                    "isError": True,
                },
            }
    return {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


__all__ = ["handle_rpc"]
