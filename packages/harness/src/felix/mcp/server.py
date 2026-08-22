"""MCP JSON-RPC surface — tools/list + tools/call via compiled manifest agents."""

from __future__ import annotations

from typing import Any

from felix import __version__
from felix.config import Settings
from felix.context import AuthContext, RequestContext, async_run_with_context
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


async def _compiled_tools(
    *,
    settings: Settings,
    tools: ToolProvider,
    auth: AuthContext,
    manifest_name: str,
) -> tuple[Any, list[Any]]:
    """Resolve + compile the tenant manifest; return (agent, governed tools)."""
    from felix.runtime import build_tenant_agent, prepare_tenant_invoke, resolve_tenant_manifest

    resolved = await resolve_tenant_manifest(
        settings, auth.tenant_id, manifest_name, thread_id=None
    )
    await prepare_tenant_invoke(
        settings, resolved=resolved, auth=auth, thread_id=None
    )
    agent = await build_tenant_agent(
        settings,
        manifest=resolved.manifest,
        tools=tools,
        tenant_id=auth.tenant_id,
    )
    agent_tools = list(getattr(agent, "tools", None) or [])
    return agent, agent_tools


async def handle_rpc(
    *,
    settings: Settings,
    tools: ToolProvider,
    method: str,
    params: dict[str, Any],
    rpc_id: str | int | None,
    auth: AuthContext | None = None,
) -> dict[str, Any]:
    call_auth = auth or AuthContext()
    manifest_name = str(
        params.get("manifest") or getattr(settings, "default_manifest", "quick") or "quick"
    )

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

    if method in {"tools/list", "tools/call"}:
        try:
            req_ctx = RequestContext(
                settings=settings,
                auth=call_auth,
                manifest_id=manifest_name,
            )
            async with async_run_with_context(req_ctx):
                _agent, agent_tools = await _compiled_tools(
                    settings=settings,
                    tools=tools,
                    auth=call_auth,
                    manifest_name=manifest_name,
                )
                by_name = {t.name: t for t in agent_tools}

                if method == "tools/list":
                    return {
                        "jsonrpc": "2.0",
                        "id": rpc_id,
                        "result": {
                            "tools": [_tool_descriptor(t) for t in agent_tools],
                        },
                    }

                name = str(params.get("name") or "")
                args = params.get("arguments") or {}
                if not name or name not in by_name:
                    return {
                        "jsonrpc": "2.0",
                        "id": rpc_id,
                        "error": {"code": -32602, "message": f"Unknown tool: {name}"},
                    }
                tool = by_name[name]
                try:
                    out = await tool.executor.execute(
                        dict(args) if isinstance(args, dict) else {},
                        ToolInvocationCtx(manifest_id=manifest_name),
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
        except Exception as exc:
            from felix.manifests.inbound_auth import InboundAuthError

            if isinstance(exc, InboundAuthError):
                return {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "error": {"code": -32001, "message": exc.detail},
                }
            return {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "error": {"code": -32000, "message": str(exc)},
            }

    return {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


__all__ = ["handle_rpc"]
