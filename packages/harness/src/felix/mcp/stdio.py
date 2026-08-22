"""MCP stdio transport — JSON-RPC over a short-lived subprocess."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from felix.manifests.schema import McpServerRef

logger = logging.getLogger("felix.mcp.stdio")

_RPC_ID = 0


def _next_id() -> int:
    global _RPC_ID
    _RPC_ID += 1
    return _RPC_ID


async def _write_message(proc: asyncio.subprocess.Process, payload: dict[str, Any]) -> None:
    if proc.stdin is None:
        raise RuntimeError("MCP stdio process has no stdin")
    raw = json.dumps(payload).encode("utf-8")
    header = f"Content-Length: {len(raw)}\r\n\r\n".encode("ascii")
    proc.stdin.write(header + raw)
    await proc.stdin.drain()


async def _read_message(
    proc: asyncio.subprocess.Process, *, wait_s: float = 30.0
) -> dict[str, Any]:
    if proc.stdout is None:
        raise RuntimeError("MCP stdio process has no stdout")

    async def _read_headers() -> tuple[int, bytes]:
        buf = b""
        while True:
            chunk = await proc.stdout.read(1)
            if not chunk:
                raise RuntimeError("MCP stdio EOF while reading headers")
            buf += chunk
            if buf.startswith(b"{") and buf.endswith(b"\n"):
                return -1, buf
            if b"\r\n\r\n" in buf:
                head, rest = buf.split(b"\r\n\r\n", 1)
                break
            if b"\n\n" in buf:
                head, rest = buf.split(b"\n\n", 1)
                break
        length = 0
        for line in head.splitlines():
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":", 1)[1].strip())
        return length, rest

    length, rest = await asyncio.wait_for(_read_headers(), timeout=wait_s)
    if length < 0:
        return json.loads(rest.decode("utf-8"))
    body = rest
    while len(body) < length:
        chunk = await asyncio.wait_for(proc.stdout.read(length - len(body)), timeout=wait_s)
        if not chunk:
            raise RuntimeError("MCP stdio EOF while reading body")
        body += chunk
    return json.loads(body[:length].decode("utf-8"))


async def stdio_rpc(
    ref: McpServerRef,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    wait_s: float = 30.0,
) -> dict[str, Any]:
    """Spawn ``ref.command``, handshake, call ``method``, then terminate."""
    if not ref.command:
        raise RuntimeError("stdio MCP ref has no command")
    env = os.environ.copy()
    env.update(ref.env or {})
    proc = await asyncio.create_subprocess_exec(
        ref.command,
        *list(ref.args),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=ref.cwd or None,
        env=env,
        limit=1024 * 1024,
    )
    try:
        await _write_message(
            proc,
            {
                "jsonrpc": "2.0",
                "id": _next_id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "felix", "version": "0.1.0"},
                },
            },
        )
        await _read_message(proc, wait_s=wait_s)
        await _write_message(
            proc,
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        )
        if method == "initialize":
            return {}
        await _write_message(
            proc,
            {
                "jsonrpc": "2.0",
                "id": _next_id(),
                "method": method,
                "params": params or {},
            },
        )
        body = await _read_message(proc, wait_s=wait_s)
        if body.get("error"):
            err = body["error"]
            raise RuntimeError(f"MCP stdio error {err.get('code')}: {err.get('message')}")
        result = body.get("result") if "result" in body else body
        if not isinstance(result, dict):
            return {"result": result}
        return result
    finally:
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except TimeoutError:
                proc.kill()
                await proc.wait()


async def list_stdio_tools(ref: McpServerRef) -> list[dict[str, Any]]:
    try:
        result = await stdio_rpc(ref, "tools/list", {})
    except Exception:
        logger.warning("MCP stdio tools/list failed for %s", ref.name, exc_info=True)
        return []
    tools = result.get("tools") if isinstance(result, dict) else None
    if not isinstance(tools, list):
        return []
    return [t for t in tools if isinstance(t, dict) and t.get("name")]


__all__ = ["list_stdio_tools", "stdio_rpc"]
