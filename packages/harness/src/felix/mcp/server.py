"""MCP JSON-RPC surface — tools/list + tools/call via compiled manifest agents."""

from __future__ import annotations

import logging
from typing import Any

from felix import __version__
from felix.config import Settings
from felix.context import AuthContext, RequestContext, async_run_with_context
from felix.errors import client_safe_message, internal_error_message
from felix.logging_setup import loggable
from felix.tools.errors import read_tool_error_code
from felix.tools.provider import ToolProvider
from felix.tools.types import (
    ToolInvocationCtx,
    ToolOutput,
    is_wrapper_deny,
    tool_output_content,
    tool_output_images,
)

logger = logging.getLogger("felix.mcp.server")


class _UnknownManifest(LookupError):
    """The manifest the caller named does not resolve. Its message names only that manifest, so it
    is the one resolution failure relayed; any other `LookupError` (a `KeyError` from deep in a
    compile) carries whatever key it was, and is not."""


def _tool_descriptor(tool: Any) -> dict[str, Any]:
    from felix.tools.types import tool_input_schema

    return {
        "name": tool.name,
        "description": tool.description,
        "inputSchema": tool_input_schema(tool),
    }


async def _compiled_tools(
    *,
    settings: Settings,
    tools: ToolProvider,
    auth: AuthContext,
    manifest_name: str,
) -> tuple[list[Any], Any]:
    """Resolve + compile the tenant manifest; return (governed tools, manifest)."""
    from felix.runtime import governed_tools, resolve_tenant_manifest

    try:
        resolved = await resolve_tenant_manifest(settings, auth.tenant_id, manifest_name, thread_id=None)
    except LookupError as exc:
        raise _UnknownManifest(f"Unknown manifest: {manifest_name}") from exc
    agent_tools = await governed_tools(settings, resolved=resolved, auth=auth, tools=tools)
    return agent_tools, resolved.manifest


async def _image_blocks(out: ToolOutput) -> list[dict[str, Any]]:
    """A tool's images as MCP `image` content: base64 bytes, never a reference or a URL.

    `tools/call` runs without the runner, so `outgoing_tool_images` applies what the runner
    would: the per-call cap, the upload rules on inline bytes, and only stored images this
    request made -- an MCP client cannot resolve a reference, and one a tool merely names may be
    any upload in the tenant. The images are the governed output's: the screening wrappers ran
    in `tool.executor`.
    """
    import base64

    from felix.tools.tool_images import outgoing_tool_images

    return [
        {"type": "image", "data": base64.b64encode(raw).decode("ascii"), "mimeType": media_type}
        for media_type, raw in await outgoing_tool_images(tool_output_images(out))
    ]


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
    manifest_name = str(params.get("manifest") or getattr(settings, "default_manifest", "quick") or "quick")

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
                agent_tools, manifest = await _compiled_tools(
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
                # A user turn is screened at every HTTP ingress; a tool call made directly
                # over MCP has no turn, so the arguments are what gets screened.
                from felix.governance.screening import InboundScreeningError
                from felix.governance.tool_screening import screen_tool_arguments

                try:
                    args = await screen_tool_arguments(
                        manifest, dict(args) if isinstance(args, dict) else {}, settings
                    )
                except InboundScreeningError as exc:
                    return {
                        "jsonrpc": "2.0",
                        "id": rpc_id,
                        "error": {"code": -32602, "message": f"arguments refused: {exc.detail}"},
                    }
                try:
                    out = await tool.executor.execute(args, ToolInvocationCtx(manifest_id=manifest_name))
                    # A tool's own marked error is a failed call too, not only a governance deny.
                    failed = is_wrapper_deny(out) or read_tool_error_code(out) is not None
                    return {
                        "jsonrpc": "2.0",
                        "id": rpc_id,
                        "result": {
                            "content": [
                                {"type": "text", "text": tool_output_content(out)},
                                *await _image_blocks(out),
                            ],
                            "isError": failed,
                        },
                    }
                except Exception:
                    # A raise from the tool, not a marked error it returned: its text is the
                    # tool's internals, not an answer written for the caller.
                    logger.exception("mcp tools/call: %s raised", loggable(name, limit=128))
                    return {
                        "jsonrpc": "2.0",
                        "id": rpc_id,
                        "result": {
                            "content": [{"type": "text", "text": f"error: {internal_error_message()}"}],
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
            if isinstance(exc, _UnknownManifest):
                return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32004, "message": str(exc)}}
            logger.exception("mcp %s failed", loggable(method, limit=32))
            return {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "error": {"code": -32000, "message": client_safe_message(exc)},
            }

    return {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


__all__ = ["handle_rpc"]
