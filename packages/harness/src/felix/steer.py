"""In-flight message queue — steer vs follow-up while an agent run is active.

Uses Redis lists when available so steer works across API replicas.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

from felix.redis_conn import RedisConnection

logger = logging.getLogger("felix.steer")


class QueueKind(StrEnum):
    STEER = "steer"
    FOLLOW_UP = "follow_up"


@dataclass(slots=True)
class QueuedMessage:
    kind: QueueKind
    text: str


@dataclass
class _RunQueue:
    steer: asyncio.Queue[QueuedMessage] = field(default_factory=asyncio.Queue)
    follow_up: asyncio.Queue[QueuedMessage] = field(default_factory=asyncio.Queue)
    cancel_remaining_tools: bool = False
    aborted: bool = False


_queues: dict[str, _RunQueue] = {}
_conn = RedisConnection("steer")


def _key(tenant_id: str, thread_id: str) -> str:
    return f"{tenant_id}:{thread_id}"


def _redis_key(tenant_id: str, thread_id: str, kind: str) -> str:
    return f"felix:steer:{tenant_id}:{thread_id}:{kind}"


async def ensure_run_queue(tenant_id: str, thread_id: str) -> _RunQueue:
    """The in-process queue for a run, created on first use.

    No lock. There is no `await` between the check and the insert, so asyncio cannot
    interleave another task into the middle of one. The `asyncio.Lock` this replaces was
    worse than unnecessary: its waiters bind to the loop that created them, so a process
    using more than one event loop can leave it held by a loop that no longer exists.
    """
    k = _key(tenant_id, thread_id)
    if k not in _queues:
        _queues[k] = _RunQueue()
    return _queues[k]


async def enqueue(
    tenant_id: str,
    thread_id: str,
    *,
    kind: Literal["steer", "follow_up"] | QueueKind,
    text: str,
) -> dict[str, str]:
    qk = QueueKind(kind) if not isinstance(kind, QueueKind) else kind
    client = await _conn.get()
    if client is not None:
        try:
            payload = json.dumps({"kind": qk.value, "text": text})
            await client.rpush(_redis_key(tenant_id, thread_id, qk.value), payload)
            if qk is QueueKind.STEER:
                await client.set(
                    _redis_key(tenant_id, thread_id, "cancel"),
                    "1",
                    ex=3600,
                )
            await client.expire(_redis_key(tenant_id, thread_id, qk.value), 3600)
            return {"queued": qk.value, "thread_id": thread_id}
        except Exception:
            logger.debug("steer redis enqueue failed", exc_info=True)

    q = await ensure_run_queue(tenant_id, thread_id)
    msg = QueuedMessage(kind=qk, text=text)
    if qk is QueueKind.STEER:
        q.cancel_remaining_tools = True
        await q.steer.put(msg)
    else:
        await q.follow_up.put(msg)
    return {"queued": qk.value, "thread_id": thread_id}


async def request_abort(tenant_id: str, thread_id: str) -> dict[str, Any]:
    """Abort the active run: cancel remaining tools and mark the queue aborted."""
    client = await _conn.get()
    if client is not None:
        try:
            await client.set(_redis_key(tenant_id, thread_id, "cancel"), "1", ex=3600)
            await client.set(_redis_key(tenant_id, thread_id, "abort"), "1", ex=3600)
        except Exception:
            logger.debug("abort redis set failed", exc_info=True)
    q = await ensure_run_queue(tenant_id, thread_id)
    q.cancel_remaining_tools = True
    q.aborted = True
    try:
        from felix.context import try_get_context

        ctx = try_get_context()
        if ctx is not None:
            ctx.limit_state.aborted = True
    except Exception:
        pass
    return {"ok": True, "aborted": True, "thread_id": thread_id}


async def is_aborted(tenant_id: str, thread_id: str) -> bool:
    client = await _conn.get()
    if client is not None:
        try:
            if await client.get(_redis_key(tenant_id, thread_id, "abort")):
                return True
        except Exception:
            logger.debug("abort redis read failed", exc_info=True)
    q = await ensure_run_queue(tenant_id, thread_id)
    return q.aborted


async def clear_abort(tenant_id: str, thread_id: str) -> None:
    client = await _conn.get()
    if client is not None:
        with contextlib.suppress(Exception):
            await client.delete(_redis_key(tenant_id, thread_id, "abort"))
    q = await ensure_run_queue(tenant_id, thread_id)
    q.aborted = False


async def peek_steer_count(tenant_id: str, thread_id: str) -> int:
    """Non-destructive count of queued steer messages (best-effort)."""
    q = await ensure_run_queue(tenant_id, thread_id)
    n = q.steer.qsize()
    client = await _conn.get()
    if client is not None:
        with contextlib.suppress(Exception):
            n += int(await client.llen(_redis_key(tenant_id, thread_id, QueueKind.STEER.value)))
    return n


async def _drain_redis(tenant_id: str, thread_id: str, kind: str) -> list[QueuedMessage]:
    client = await _conn.get()
    if client is None:
        return []
    out: list[QueuedMessage] = []
    rkey = _redis_key(tenant_id, thread_id, kind)
    try:
        while True:
            raw = await client.lpop(rkey)
            if raw is None:
                break
            data = json.loads(raw)
            out.append(QueuedMessage(kind=QueueKind(data["kind"]), text=str(data["text"])))
    except Exception:
        logger.debug("steer redis drain failed", exc_info=True)
    return out


async def drain_steer(
    tenant_id: str,
    thread_id: str,
    *,
    mode: Literal["all", "one-at-a-time"] = "all",
) -> list[QueuedMessage]:
    remote = await _drain_redis(tenant_id, thread_id, QueueKind.STEER.value)
    q = await ensure_run_queue(tenant_id, thread_id)
    out: list[QueuedMessage] = list(remote)
    while not q.steer.empty():
        try:
            out.append(q.steer.get_nowait())
        except asyncio.QueueEmpty:
            break
    if mode == "one-at-a-time" and out:
        # Re-queue the rest
        rest = out[1:]
        out = out[:1]
        for msg in rest:
            await q.steer.put(msg)
    return out


async def drain_follow_up(
    tenant_id: str,
    thread_id: str,
    *,
    mode: Literal["all", "one-at-a-time"] = "all",
) -> list[QueuedMessage]:
    remote = await _drain_redis(tenant_id, thread_id, QueueKind.FOLLOW_UP.value)
    q = await ensure_run_queue(tenant_id, thread_id)
    out: list[QueuedMessage] = list(remote)
    while not q.follow_up.empty():
        try:
            out.append(q.follow_up.get_nowait())
        except asyncio.QueueEmpty:
            break
    if mode == "one-at-a-time" and out:
        rest = out[1:]
        out = out[:1]
        for msg in rest:
            await q.follow_up.put(msg)
    return out


async def should_cancel_remaining_tools(tenant_id: str, thread_id: str) -> bool:
    if await is_aborted(tenant_id, thread_id):
        return True
    client = await _conn.get()
    if client is not None:
        try:
            return bool(await client.get(_redis_key(tenant_id, thread_id, "cancel")))
        except Exception:
            logger.debug("steer redis cancel read failed", exc_info=True)
    q = await ensure_run_queue(tenant_id, thread_id)
    return q.cancel_remaining_tools


async def clear_cancel_flag(tenant_id: str, thread_id: str) -> None:
    client = await _conn.get()
    if client is not None:
        try:
            await client.delete(_redis_key(tenant_id, thread_id, "cancel"))
        except Exception:
            logger.debug("steer redis cancel clear failed", exc_info=True)
    q = await ensure_run_queue(tenant_id, thread_id)
    q.cancel_remaining_tools = False


async def release_run_queue(tenant_id: str, thread_id: str) -> None:
    # Same reasoning as `ensure_run_queue`: a single dict operation, no `await` in the
    # middle of it, so there is nothing for a lock to serialise.
    _queues.pop(_key(tenant_id, thread_id), None)


__all__ = [
    "QueueKind",
    "QueuedMessage",
    "clear_abort",
    "clear_cancel_flag",
    "drain_follow_up",
    "drain_steer",
    "enqueue",
    "ensure_run_queue",
    "is_aborted",
    "peek_steer_count",
    "release_run_queue",
    "request_abort",
    "should_cancel_remaining_tools",
]
