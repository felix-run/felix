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


# Where a side event is also recorded, so a caller with no stream can still be told.
REQUEST_EXTRA = "side_events"


def _record_on_request(event: str, data: dict[str, Any]) -> None:
    """Note the event on the active request, for a caller that has no stream to read it from.

    The queue below reaches only a live SSE consumer. A non-streaming `/chat` never drains it,
    so a run blocked on an approval held the caller for the rule's whole TTL and then answered
    with a denial, and nothing in the response said an approval had ever been asked for. The
    request context outlives the run and is shared by every agent the request compiles, so
    the route reads the record back once `invoke` returns.
    """
    from felix.context import try_get_context

    ctx = try_get_context()
    if ctx is not None:
        ctx.extras.setdefault(REQUEST_EXTRA, []).append({"event": event, "data": dict(data)})


async def emit(thread_id: str | None, event: str, data: dict[str, Any]) -> None:
    """Publish an event for the active SSE consumer of ``thread_id``, and record it on the
    request."""
    _record_on_request(event, data)
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


def requested_on(extras: dict[str, Any], event: str) -> list[dict[str, Any]]:
    """The payloads of every `event` recorded on a request's `extras`, in order."""
    return [dict(e["data"]) for e in extras.get(REQUEST_EXTRA, []) if e.get("event") == event]


__all__ = ["REQUEST_EXTRA", "drain", "emit", "ensure_queue", "release", "requested_on"]
