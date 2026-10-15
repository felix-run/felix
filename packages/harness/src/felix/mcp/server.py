"""MCP JSON-RPC surface — tools/list + tools/call via compiled manifest agents."""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any

from felix import __version__
from felix.config import Settings
from felix.context import AuthContext, RequestContext, async_run_with_context
from felix.errors import client_safe_message
from felix.logging_setup import loggable
from felix.tools.provider import ToolProvider

if TYPE_CHECKING:
    from felix.patterns.types import ImageAttachment

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


async def _image_blocks(images: list[ImageAttachment]) -> list[dict[str, Any]]:
    """A tool's images as MCP `image` content: base64 bytes, never a reference or a URL.

    The runner hands them back unstored (`ToolRunner.for_client`), so `outgoing_tool_images`
    applies what storing them would: the per-call cap, the upload rules on inline bytes, and
    only stored images this request made -- an MCP client cannot resolve a reference, and one a
    tool merely names may be any upload in the tenant. They are the governed output's: the
    screening wrappers ran in `tool.executor`, and a hook that rewrote the text dropped them.
    """
    import base64

    from felix.tools.tool_images import outgoing_tool_images

    return [
        {"type": "image", "data": base64.b64encode(raw).decode("ascii"), "mimeType": media_type}
        for media_type, raw in await outgoing_tool_images(images)
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
                # Through the agent loop's own dispatch, as a workflow's tool step is: the
                # governed executor, and around it the plugin `before_tool` / `after_tool` hooks,
                # the manifest's `pre_tool_use` / `post_tool_use`, the `tool_call` audit row, the
                # counters and the span. Called straight on the executor, an MCP client's call
                # was the one way to run a tool that none of those saw.
                from felix.manifest_hooks import ManifestHooks
                from felix.patterns.tool_runner import ToolRunner
                from felix.patterns.types import ToolCall
                from felix.tools.types import is_failure_content

                hooks = getattr(getattr(manifest, "spec", None), "hooks", None)
                runner = ToolRunner(
                    tool_map={name: tool},
                    manifest_id=manifest_name,
                    manifest_hooks=ManifestHooks(list(hooks), manifest.metadata.name) if hooks else None,
                    for_client=True,
                )
                call = ToolCall(id=f"mcp-{uuid.uuid4().hex[:12]}", name=name, args=args)
                _fatal, message, _terminate, denied = await runner.dispatch(call, None)
                text = str(message.content or "")
                return {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "result": {
                        "content": [
                            {"type": "text", "text": text},
                            *await _image_blocks(message.attachments or []),
                        ],
                        # A refusal, a raise, or a tool's own marked error: each is a failed call.
                        "isError": denied or is_failure_content(text),
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
