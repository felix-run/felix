"""Felix's MCP server: a compiled manifest's governed tools, served by the official `mcp` SDK.

The SDK owns the protocol -- both eras (the legacy `initialize` handshake and the `2026-07-28`
per-request envelope with `server/discover`), header validation, status codes and notifications.
This module owns what is Felix's: which manifest a request means, compiling its governed tools as
the caller, screening a call's arguments, and dispatching through `ToolRunner`.

Every request is stateless (`StreamableHTTPSessionManager(stateless=True)`), so any replica can
answer any request and nothing outlives the HTTP exchange. The route hands the request its target
through `_TARGET`; the SDK runs the handler inside that call, so the context var carries over.
"""

from __future__ import annotations

import base64
import logging
import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING, Any

from mcp.server.caching import CacheHint
from mcp.server.lowlevel import Server
from mcp.shared.exceptions import MCPError
from mcp_types import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    CallToolRequestParams,
    CallToolResult,
    ImageContent,
    ListToolsResult,
    TextContent,
    Tool,
)

from felix import __version__
from felix.config import Settings
from felix.context import AuthContext, RequestContext, async_run_with_context, try_get_context
from felix.errors import client_safe_message
from felix.logging_setup import loggable
from felix.tools.provider import ToolProvider

if TYPE_CHECKING:
    from mcp.server.context import ServerRequestContext

    from felix.patterns.types import ImageAttachment

logger = logging.getLogger("felix.mcp.server")

# Implementation-defined codes this server answered with before the SDK, kept so a client that
# keyed on them still can. Both sit in the legacy sub-range (-32000..-32019) the spec leaves alone.
INBOUND_AUTH_REFUSED = -32001
UNKNOWN_MANIFEST = -32004


@dataclass(frozen=True, slots=True)
class McpTarget:
    """What one `/mcp` request is for: the app's settings and tools, and the manifest it named."""

    settings: Settings
    tools: ToolProvider
    manifest: str | None = None


_TARGET: ContextVar[McpTarget | None] = ContextVar("felix_mcp_target", default=None)


@dataclass(frozen=True, slots=True)
class _McpCall:
    """What a handler's work runs with: the request's target, its caller, and the manifest meant."""

    settings: Settings
    tools: ToolProvider
    auth: AuthContext
    manifest_name: str


class _UnknownManifest(LookupError):
    """The manifest the caller named does not resolve. Its message names only that manifest, so it
    is the one resolution failure relayed; any other `LookupError` (a `KeyError` from deep in a
    compile) carries whatever key it was, and is not."""


def _tool_descriptor(tool: Any) -> Tool:
    from felix.tools.types import tool_input_schema

    return Tool(name=tool.name, description=tool.description, input_schema=tool_input_schema(tool))


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


async def _image_blocks(images: list[ImageAttachment]) -> list[ImageContent]:
    """A tool's images as MCP `image` content: base64 bytes, never a reference or a URL.

    The runner hands them back unstored (`ToolRunner.for_client`), so `outgoing_tool_images`
    applies what storing them would: the per-call cap, the upload rules on inline bytes, and
    only stored images this request made -- an MCP client cannot resolve a reference, and one a
    tool merely names may be any upload in the tenant. They are the governed output's: the
    screening wrappers ran in `tool.executor`, and a hook that rewrote the text dropped them.
    """
    from felix.tools.tool_images import outgoing_tool_images

    return [
        ImageContent(type="image", data=base64.b64encode(raw).decode("ascii"), mime_type=media_type)
        for media_type, raw in await outgoing_tool_images(images)
    ]


def _manifest_name(target: McpTarget, params: Any) -> str:
    """The path's manifest, else the legacy `params.manifest`, else the deployment's default."""
    named = target.manifest or (params.get("manifest") if isinstance(params, dict) else None)
    return str(named or getattr(target.settings, "default_manifest", "quick") or "quick")


async def _with_manifest[T](method: str, params: Any, work: Callable[[_McpCall], Awaitable[T]]) -> T:
    """Run ``work`` under the request's context, turning Felix's failures into the JSON-RPC errors
    a client is told."""
    from felix.manifests.inbound_auth import InboundAuthError

    target = _TARGET.get()
    if target is None:
        raise MCPError(INTERNAL_ERROR, "MCP request reached the server without a target")
    ctx = try_get_context()
    if ctx is None:
        # `AuthMiddleware` installs one around every request. Compiling without it would be
        # compiling as an anonymous caller on the default tenant, whoever actually asked.
        raise MCPError(INTERNAL_ERROR, "MCP request reached the server without a caller")
    auth = ctx.auth
    manifest_name = _manifest_name(target, params)
    try:
        req_ctx = RequestContext(settings=target.settings, auth=auth, manifest_id=manifest_name)
        async with async_run_with_context(req_ctx):
            return await work(_McpCall(target.settings, target.tools, auth, manifest_name))
    except MCPError:
        raise
    except InboundAuthError as exc:
        raise MCPError(INBOUND_AUTH_REFUSED, exc.detail) from None
    except _UnknownManifest as exc:
        raise MCPError(UNKNOWN_MANIFEST, str(exc)) from None
    except Exception as exc:
        logger.exception("mcp %s failed", loggable(method, limit=32))
        raise MCPError(INTERNAL_ERROR, client_safe_message(exc)) from None


def _compile(call: _McpCall) -> Awaitable[tuple[list[Any], Any]]:
    return _compiled_tools(
        settings=call.settings, tools=call.tools, auth=call.auth, manifest_name=call.manifest_name
    )


async def _list_tools(ctx: ServerRequestContext[Any], _params: Any) -> ListToolsResult:
    async def work(call: _McpCall) -> ListToolsResult:
        agent_tools, _manifest = await _compile(call)
        return ListToolsResult(tools=[_tool_descriptor(t) for t in agent_tools])

    return await _with_manifest("tools/list", ctx.params, work)


async def _call_tool(ctx: ServerRequestContext[Any], params: CallToolRequestParams) -> CallToolResult:
    async def work(call: _McpCall) -> CallToolResult:
        agent_tools, manifest = await _compile(call)
        by_name = {t.name: t for t in agent_tools}
        name = str(params.name or "")
        if name not in by_name:
            raise MCPError(INVALID_PARAMS, f"Unknown tool: {name}")
        tool = by_name[name]
        args = params.arguments or {}
        # A user turn is screened at every HTTP ingress; a tool call made directly
        # over MCP has no turn, so the arguments are what gets screened.
        from felix.governance.screening import InboundScreeningError
        from felix.governance.tool_screening import screen_tool_arguments

        try:
            args = await screen_tool_arguments(
                manifest, dict(args) if isinstance(args, dict) else {}, call.settings
            )
        except InboundScreeningError as exc:
            raise MCPError(INVALID_PARAMS, f"arguments refused: {exc.detail}") from None
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
            manifest_id=call.manifest_name,
            manifest_hooks=ManifestHooks(list(hooks), manifest.metadata.name) if hooks else None,
            for_client=True,
        )
        tool_call = ToolCall(id=f"mcp-{uuid.uuid4().hex[:12]}", name=name, args=args)
        _fatal, message, _terminate, denied = await runner.dispatch(tool_call, None)
        text = str(message.content or "")
        return CallToolResult(
            content=[TextContent(type="text", text=text), *await _image_blocks(message.attachments or [])],
            # A refusal, a raise, or a tool's own marked error: each is a failed call.
            is_error=denied or is_failure_content(text),
        )

    return await _with_manifest("tools/call", ctx.params, work)


@cache
def mcp_server() -> Server[Any]:
    """The one SDK `Server` every request is served by; it holds no per-request state."""
    server: Server[Any] = Server(
        "felix",
        version=__version__,
        # What a manifest compiles to depends on who asks, so no cache may be shared across
        # callers; a short TTL because a stored manifest can change under a client.
        cache_hints={
            "server/discover": CacheHint(ttl_ms=300_000, scope="private"),
            "tools/list": CacheHint(ttl_ms=60_000, scope="private"),
        },
        # Without a lookup the SDK finds a called tool's schema, to check `Mcp-Param-*` headers, by
        # running `tools/list` first: a second full compile on every call. Felix routes on no such
        # header, and dispatch rejects an unknown tool itself.
        get_tool_input_schema=lambda _name: None,
        on_list_tools=_list_tools,
        on_call_tool=_call_tool,
    )
    # The SDK's default OpenTelemetry middleware parents spans on a client-supplied `traceparent`
    # and names them after any tool name a client sends; `ToolRunner` already traces every call.
    server.middleware = []
    return server


async def serve_mcp(target: McpTarget, scope: Any, receive: Any, send: Any) -> None:
    """Answer one `/mcp` HTTP exchange as the SDK's stateless Streamable HTTP transport.

    A session manager per exchange: in stateless mode it holds nothing between requests, and
    owning its run here (rather than in the app lifespan) means an app that never ran its lifespan
    -- every ASGI test -- serves MCP exactly as production does.
    """
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    manager = StreamableHTTPSessionManager(app=mcp_server(), stateless=True, json_response=True)
    token = _TARGET.set(target)
    try:
        async with manager.run():
            await manager.handle_request(scope, receive, send)
    finally:
        _TARGET.reset(token)


__all__ = ["McpTarget", "mcp_server", "serve_mcp"]
