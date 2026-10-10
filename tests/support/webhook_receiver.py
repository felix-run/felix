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
