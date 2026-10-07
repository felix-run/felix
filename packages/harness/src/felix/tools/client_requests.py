"""Client-tool requests a run is waiting on, kept where another process can read them.

A client tool announces itself with a `tool_request` side event, and `felix.side_events` is
an in-process queue. On a streamed run that is enough: the agent and the SSE response share a
process. On a durable run it is not — the agent is in the worker and the stream is served by
the API — so the request was emitted into a queue nobody drains, and the run waited out the
tool's whole timeout with no client ever asked. `cowork` is durable and binds `local_shell`,
so on a deployment with a worker its client tools could never run.

Approvals had the same gap and closed it by re-deriving the frame from a durable record (the
approvals row) rather than forwarding a message; this is that, for client tools. The executor
records the request here before it waits and clears it once it is answered or abandoned, and
the durable stream reads what is pending on its thread and announces each request once.

Redis, because the answer already travels through Redis (`felix.waiters`) and a request is
exactly as long-lived as the wait for it: one hash per thread, a field per call. Without Redis
it falls back to this process, which is still correct wherever the run and the stream share
one, and is no worse than before wherever they do not.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from felix.redis_conn import RedisConnection
from felix.waiters import waiter_name

logger = logging.getLogger("felix.tools.client_requests")

_PREFIX = "felix:client_pending:"

#: Seconds a thread's hash outlives its newest request's deadline. Only a safety margin: a
#: request is cleared when its wait ends, and a lapsed one is skipped on read regardless.
_EXPIRY_MARGIN_SECONDS = 60

_local: dict[str, dict[str, str]] = {}
_lock = asyncio.Lock()
_conn = RedisConnection(
    "client_requests",
    fallback_consequence="a durable run's client tools are never announced to the stream serving it",
)


def _key(thread_id: str) -> str:
    # `waiter_name` for the same reason the waiter itself uses it: a thread id may carry
    # colons (`{tenant}:fiber:{id}`), and the key must not let one thread name another's.
    return f"{_PREFIX}{waiter_name('client_pending', thread_id)}"


async def record(thread_id: str, request: dict[str, Any], *, timeout: float) -> None:
    """Note that `request` (a `tool_request` payload) is waiting on its client. Never raises."""
    tool_call_id = str(request.get("id") or "")
    if not thread_id or not tool_call_id:
        return
    expires_at = int((time.time() + timeout) * 1000)
    raw = json.dumps({**request, "expires_at": expires_at}, default=str)
    client = await _conn.get()
    if client is not None:
        try:
            key = _key(thread_id)
            await client.hset(key, tool_call_id, raw)
            await client.expire(key, int(timeout) + _EXPIRY_MARGIN_SECONDS)
            return
        except Exception:
            await _conn.fallback("client request redis hset")
    async with _lock:
        _local.setdefault(_key(thread_id), {})[tool_call_id] = raw


async def clear(thread_id: str, tool_call_id: str) -> None:
    """Forget a request once its wait has ended, answered or not. Never raises."""
    if not thread_id or not tool_call_id:
        return
    client = await _conn.get()
    if client is not None:
        try:
            await client.hdel(_key(thread_id), tool_call_id)
        except Exception:
            await _conn.fallback("client request redis hdel")
    # Both, unconditionally: a request recorded while Redis was down lives here, and a
    # connection that came back since must not strand it.
    async with _lock:
        held = _local.get(_key(thread_id))
        if held is not None:
            held.pop(tool_call_id, None)
            if not held:
                _local.pop(_key(thread_id), None)


async def pending(thread_id: str) -> list[dict[str, Any]]:
    """The requests waiting on `thread_id`'s client, oldest deadline first. Never raises.

    Lapsed ones are left out: a request whose wait has timed out has already been answered
    with `[error/timeout]`, and announcing it would ask the client for a result nobody reads.
    """
    if not thread_id:
        return []
    raws: list[Any] = []
    client = await _conn.get()
    if client is not None:
        try:
            raws.extend((await client.hgetall(_key(thread_id))).values())
        except Exception:
            await _conn.fallback("client request redis hgetall")
    async with _lock:
        raws.extend((_local.get(_key(thread_id)) or {}).values())

    now = time.time() * 1000
    out: dict[str, dict[str, Any]] = {}
    for raw in raws:
        try:
            request = json.loads(raw)
        except TypeError, ValueError:
            continue
        if not isinstance(request, dict) or not request.get("id"):
            continue
        try:
            if float(request.get("expires_at") or 0) < now:
                continue
        except TypeError, ValueError:
            continue
        out[str(request["id"])] = request
    return sorted(out.values(), key=lambda r: float(r.get("expires_at") or 0))


__all__ = ["clear", "pending", "record"]
