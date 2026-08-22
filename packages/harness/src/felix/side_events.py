"""Side-channel events emitted while a tool call is blocked (approvals, client tools)."""

from __future__ import annotations

import asyncio
from typing import Any

_queues: dict[str, asyncio.Queue[dict[str, Any]]] = {}
_lock = asyncio.Lock()


async def ensure_queue(thread_id: str) -> asyncio.Queue[dict[str, Any]]:
    async with _lock:
        q = _queues.get(thread_id)
        if q is None:
            q = asyncio.Queue()
            _queues[thread_id] = q
        return q


async def emit(thread_id: str | None, event: str, data: dict[str, Any]) -> None:
    """Publish an event for the active SSE consumer of ``thread_id``."""
    if not thread_id:
        return
    q = await ensure_queue(thread_id)
    await q.put({"event": event, "data": data})


async def drain(thread_id: str | None, *, max_items: int = 32) -> list[dict[str, Any]]:
    if not thread_id:
        return []
    async with _lock:
        q = _queues.get(thread_id)
    if q is None:
        return []
    out: list[dict[str, Any]] = []
    while len(out) < max_items:
        try:
            out.append(q.get_nowait())
        except asyncio.QueueEmpty:
            break
    return out


async def release(thread_id: str | None) -> None:
    if not thread_id:
        return
    async with _lock:
        _queues.pop(thread_id, None)


__all__ = ["drain", "emit", "ensure_queue", "release"]
