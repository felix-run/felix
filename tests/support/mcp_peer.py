"""A real MCP server for the outbound client's tests: the official SDK's, on a loopback socket.

The client is the SDK too, so a fake that answered JSON-RPC by hand would test neither side's
idea of the protocol. `serve_http_peer` runs an SDK `MCPServer` under uvicorn on an ephemeral
loopback port and records every request's headers, so a test can assert what Felix sent (an
`authorization` that stops being sent is exactly the regression these exist to catch).

Loopback is reachable only with `allow_http=True`, the switch a development manifest uses; with
it off the egress guard refuses the URL, which is a test of its own.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from mcp_types import ImageContent

from tests.support.paths import ROOT

STDIO_PEER = ROOT / "tests" / "fixtures" / "mcp" / "stdio_peer.py"
STDIO_PYTHON = sys.executable

# A 1x1 PNG: real image bytes, so the client's content-type sniffing has something to read.
PNG_1X1 = base64.b64encode(
    bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
        "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
    )
).decode("ascii")


@dataclass
class HttpPeer:
    url: str
    headers: list[dict[str, str]] = field(default_factory=list)

    @property
    def methods(self) -> list[str]:
        """Each request's `Mcp-Method` header, in order: what a 2026-07-28 client sent."""
        return [h["mcp-method"] for h in self.headers if "mcp-method" in h]


def build_peer(*, instructions: str | None = "Use echo to repeat text back.") -> Any:
    from mcp.server.mcpserver import MCPServer

    server = MCPServer("http-peer", instructions=instructions)

    @server.tool()
    def echo(text: str) -> str:
        """Repeat the text back."""
        return f"echo:{text}"

    @server.tool()
    def fail() -> str:
        """Always fails."""
        raise ValueError("peer refused")

    @server.tool()
    def strict() -> str:
        """Refuses at the protocol level, as a JSON-RPC error rather than an error result."""
        from mcp.shared.exceptions import MCPError

        raise MCPError(-32602, "server says bad params")

    @server.tool()
    async def slow() -> str:
        """Takes longer than any timeout a test sets."""
        await asyncio.sleep(5)
        return "late"

    @server.tool()
    def picture() -> ImageContent:
        """A one-pixel image."""
        return ImageContent(type="image", data=PNG_1X1, mime_type="image/png")

    return server


@asynccontextmanager
async def serve_http_peer(server: Any | None = None, *, stateless: bool = False) -> AsyncIterator[HttpPeer]:
    """Serve ``server`` (default `build_peer()`) at `<url>` over Streamable HTTP on loopback."""
    import uvicorn

    app = (server or build_peer()).streamable_http_app(stateless_http=stateless)
    peer = HttpPeer(url="")

    async def recording(scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            peer.headers.append({k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]})
        await app(scope, receive, send)

    # A bounded graceful shutdown: a test's slow tool may still be running when the test is done.
    config = uvicorn.Config(
        recording, host="127.0.0.1", port=0, log_level="warning", lifespan="on", timeout_graceful_shutdown=1
    )
    uv = uvicorn.Server(config)
    task = asyncio.create_task(uv.serve())
    try:
        while not uv.started:
            if task.done():
                task.result()
            await asyncio.sleep(0.01)
        port = uv.servers[0].sockets[0].getsockname()[1]
        peer.url = f"http://127.0.0.1:{port}/mcp"
        yield peer
    finally:
        uv.should_exit = True
        with contextlib.suppress(asyncio.CancelledError, TimeoutError):
            await asyncio.wait_for(task, timeout=5)


__all__ = ["PNG_1X1", "STDIO_PEER", "STDIO_PYTHON", "HttpPeer", "build_peer", "serve_http_peer"]
