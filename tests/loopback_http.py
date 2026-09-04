"""A real HTTP server on loopback, for testing outbound calls through the real guard.

Shared by the fetch-tool and search-tool suites. Both need the same thing and for the same
reason: `felix.security.egress.safe_async_client` refuses `mounts` and `proxy` by
construction, so an `httpx.MockTransport` cannot be installed without routing around the
component under test. Whatever a fake said about a byte cap or an SSRF refusal, it would be
saying by fiat.

Loopback is reachable only because `allow_http=True` exempts it — the same switch a
development manifest uses — so these tests are not silently exercising nothing.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Request:
    """What the responder is told about the request.

    Headers are parsed rather than discarded because an outbound client's *headers* are as
    much a contract as its URL — an `authorization` header that silently stops being sent is
    exactly the kind of regression a test here should catch.
    """

    path: str
    headers: dict[str, str]


Responder = Callable[[Request, asyncio.StreamWriter], object]
"""(request, writer) -> None. May be a coroutine function."""


@asynccontextmanager
async def serve(responder: Responder) -> AsyncIterator[str]:
    """Run an HTTP/1.1 server on an ephemeral loopback port; yield its base URL.

    Shuts down in a `finally` rather than via `async with server`. When the caller's body
    raises — which is what a regressed byte cap does, through `asyncio.wait_for` — the
    listening socket otherwise stayed open until garbage collection, and CPython 3.14 emitted
    a deallocator `TypeError` on the way out. That made the noisiest failure also the leaky
    one, which is the worst combination for reading a test report.
    """

    async def _client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except asyncio.IncompleteReadError, ConnectionError:
            return
        text = head.decode("latin-1")
        request_line, _, header_block = text.partition("\r\n")
        parts = request_line.split(" ")
        headers = {}
        for line in header_block.split("\r\n"):
            name, sep, value = line.partition(":")
            if sep:
                headers[name.strip().lower()] = value.strip()
        request = Request(path=parts[1] if len(parts) > 1 else "/", headers=headers)
        try:
            result = responder(request, writer)
            if asyncio.iscoroutine(result):
                await result
            await writer.drain()
        except ConnectionResetError, BrokenPipeError:
            pass  # the client hit its cap or moved on; several tests depend on that
        finally:
            writer.close()

    server = await asyncio.start_server(_client, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.close()
        await server.wait_closed()


def respond(
    writer: asyncio.StreamWriter,
    body: bytes,
    *,
    status: str = "200 OK",
    ctype: str = "text/plain",
    extra: str = "",
) -> None:
    """Write one complete HTTP/1.1 response. `extra` carries additional headers, CRLF-ended."""
    head = (
        f"HTTP/1.1 {status}\r\ncontent-type: {ctype}\r\n"
        f"content-length: {len(body)}\r\n{extra}connection: close\r\n\r\n"
    ).encode()
    writer.write(head + body)


def body_of(rendered: str) -> str:
    """The part of a *rendered tool result* after its header block.

    Not HTTP headers, despite living among HTTP helpers: `http_fetch` prefixes its output
    with `status:` / `url:` / `content-type:` lines and a blank line. It exists because the
    word "bytes" appears in that preamble, so counting a marker character across the whole
    string measures the wrong thing.
    """
    _, _, body = rendered.partition("\n\n")
    return body


__all__ = ["Request", "Responder", "body_of", "respond", "serve"]
