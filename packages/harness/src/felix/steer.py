"""In-flight message queue — steer vs follow-up while an agent run is active.

Uses Redis lists when available so steer works across API replicas.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal

logger = logging.getLogger("felix.steer")


class QueueKind(str, Enum):
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


_queues: dict[str, _RunQueue] = {}
_lock = asyncio.Lock()
_redis: Any | None = None
_redis_loop: int | None = None
_redis_failed = False


def _key(tenant_id: str, thread_id: str) -> str:
    return f"{tenant_id}:{thread_id}"


def _redis_key(tenant_id: str, thread_id: str, kind: str) -> str:
    return f"felix:steer:{tenant_id}:{thread_id}:{kind}"


async def _get_redis() -> Any | None:
    global _redis, _redis_loop, _redis_failed
    loop_id = id(asyncio.get_running_loop())
    if _redis is not None and _redis_loop != loop_id:
        try:
            await _redis.aclose()
        except Exception:
            pass
        _redis = None
        _redis_loop = None
        _redis_failed = False
    if _redis_failed:
        return None
    if _redis is not None:
        return _redis
    try:
        from felix.config import get_settings

        settings = get_settings()
        url = getattr(settings, "redis_url", "") or ""
        if not url:
            return None
        import redis.asyncio as redis

        client = redis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=1.0,
            socket_timeout=2.0,
        )
        await client.ping()
        _redis = client
        _redis_loop = loop_id
        return _redis
    except Exception:
        logger.debug("steer redis unavailable; using in-process", exc_info=True)
        _redis_failed = True
        return None


async def ensure_run_queue(tenant_id: str, thread_id: str) -> _RunQueue:
    async with _lock:
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
    client = await _get_redis()
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


async def _drain_redis(tenant_id: str, thread_id: str, kind: str) -> list[QueuedMessage]:
    client = await _get_redis()
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


async def drain_steer(tenant_id: str, thread_id: str) -> list[QueuedMessage]:
    remote = await _drain_redis(tenant_id, thread_id, QueueKind.STEER.value)
    q = await ensure_run_queue(tenant_id, thread_id)
    out: list[QueuedMessage] = list(remote)
    while not q.steer.empty():
        try:
            out.append(q.steer.get_nowait())
        except asyncio.QueueEmpty:
            break
    return out


async def drain_follow_up(tenant_id: str, thread_id: str) -> list[QueuedMessage]:
    remote = await _drain_redis(tenant_id, thread_id, QueueKind.FOLLOW_UP.value)
    q = await ensure_run_queue(tenant_id, thread_id)
    out: list[QueuedMessage] = list(remote)
    while not q.follow_up.empty():
        try:
            out.append(q.follow_up.get_nowait())
        except asyncio.QueueEmpty:
            break
    return out


async def should_cancel_remaining_tools(tenant_id: str, thread_id: str) -> bool:
    client = await _get_redis()
    if client is not None:
        try:
            return bool(await client.get(_redis_key(tenant_id, thread_id, "cancel")))
        except Exception:
            logger.debug("steer redis cancel read failed", exc_info=True)
    q = await ensure_run_queue(tenant_id, thread_id)
    return q.cancel_remaining_tools


async def clear_cancel_flag(tenant_id: str, thread_id: str) -> None:
    client = await _get_redis()
    if client is not None:
        try:
            await client.delete(_redis_key(tenant_id, thread_id, "cancel"))
        except Exception:
            logger.debug("steer redis cancel clear failed", exc_info=True)
    q = await ensure_run_queue(tenant_id, thread_id)
    q.cancel_remaining_tools = False


async def release_run_queue(tenant_id: str, thread_id: str) -> None:
    async with _lock:
        _queues.pop(_key(tenant_id, thread_id), None)


__all__ = [
    "QueueKind",
    "QueuedMessage",
    "clear_cancel_flag",
    "drain_follow_up",
    "drain_steer",
    "enqueue",
    "ensure_run_queue",
    "release_run_queue",
    "should_cancel_remaining_tools",
]
