"""Drive Felix's MCP server as the SDK serves it, under a chosen caller.

`mcp_rpc` posts one JSON-RPC message through `felix.mcp.server.serve_mcp` -- the SDK's stateless
Streamable HTTP transport and Felix's handlers, the same exchange the `/mcp` route hands over --
inside a `RequestContext` carrying ``auth``, which is what `AuthMiddleware` would have installed.
A test that needs the middleware and the route too posts to `create_app()` instead.
"""

from __future__ import annotations

import json
from typing import Any

from felix.config import Settings
from felix.context import AuthContext, RequestContext, async_run_with_context
from felix.mcp.server import McpTarget, serve_mcp
from felix.tools.provider import ToolProvider


async def mcp_rpc(
    *,
    settings: Settings,
    tools: ToolProvider,
    method: str,
    params: dict[str, Any] | None = None,
    rpc_id: int | str | None = 1,
    auth: AuthContext | None = None,
    manifest: str | None = None,
    with_context: bool = True,
) -> dict[str, Any]:
    """The decoded JSON-RPC response to one legacy-era message (`{}` for an accepted notification).

    ``with_context=False`` sends it with no `RequestContext` at all, as if `AuthMiddleware` never ran.
    """
    message: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if rpc_id is not None:
        message["id"] = rpc_id
    body = json.dumps(message).encode()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/mcp",
        "raw_path": b"/mcp",
        "query_string": b"",
        "root_path": "",
        "server": ("test", 80),
        "client": ("127.0.0.1", 1),
        "headers": [
            (b"host", b"test"),
            (b"content-type", b"application/json"),
            (b"accept", b"application/json, text/event-stream"),
            (b"content-length", str(len(body)).encode()),
        ],
    }
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    status: list[int] = []
    chunks: list[bytes] = []

    async def send(event: dict[str, Any]) -> None:
        if event["type"] == "http.response.start":
            status.append(event["status"])
        elif event["type"] == "http.response.body":
            chunks.append(event.get("body", b""))

    target = McpTarget(settings=settings, tools=tools, manifest=manifest)
    if with_context:
        ctx = RequestContext(settings=settings, auth=auth or AuthContext(), manifest_id=manifest or "")
        async with async_run_with_context(ctx):
            await serve_mcp(target, scope, receive, send)
    else:
        await serve_mcp(target, scope, receive, send)
    raw = b"".join(chunks)
    assert status and status[0] in (200, 202), f"unexpected MCP HTTP status {status}: {raw[:200]!r}"
    return json.loads(raw) if raw else {}


__all__ = ["mcp_rpc"]
