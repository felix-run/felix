"""A loopback HTTP receiver that records each webhook delivery and answers with scripted statuses."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

SECRET = "a-shared-secret-long-enough"


@asynccontextmanager
async def receiver(statuses: list[int]) -> AsyncIterator[tuple[str, list[dict[str, Any]]]]:
    """An HTTP server that records each request and answers with the next status in turn."""
    seen: list[dict[str, Any]] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
        lines = head.split("\r\n")
        headers = {
            k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:] if ":" in ln)
        }
        body = await reader.readexactly(int(headers.get("content-length") or 0))
        seen.append({"path": lines[0].split(" ")[1], "headers": headers, "body": body})
        status = statuses[min(len(seen) - 1, len(statuses) - 1)]
        writer.write(f"HTTP/1.1 {status} X\r\ncontent-length: 0\r\nconnection: close\r\n\r\n".encode())
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}/hook", seen
    finally:
        server.close()
        await server.wait_closed()


@asynccontextmanager
async def hook_receiver(
    answer: Any,
) -> AsyncIterator[tuple[str, list[dict[str, Any]]]]:
    """Like `receiver`, for request/response hooks: `answer(request) -> (status, body | None)`,
    where `request` is `{"path", "headers", "body", "json"}`. A body of `...` hangs (two seconds)
    instead of answering, for a timeout."""
    import json

    seen: list[dict[str, Any]] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
        lines = head.split("\r\n")
        headers = {
            k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:] if ":" in ln)
        }
        body = await reader.readexactly(int(headers.get("content-length") or 0))
        request = {"path": lines[0].split(" ")[1], "headers": headers, "body": body, "json": json.loads(body)}
        seen.append(request)
        status, reply = answer(request)
        if reply is ...:
            # Longer than any hook's timeout in the tests, short enough not to hold teardown.
            await asyncio.sleep(2)
            return
        payload = b"" if reply is None else json.dumps(reply).encode()
        writer.write(
            f"HTTP/1.1 {status} X\r\ncontent-type: application/json\r\ncontent-length: {len(payload)}\r\n"
            f"connection: close\r\n\r\n".encode()
            + payload
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}/hook", seen
    finally:
        server.close()
        await server.wait_closed()
