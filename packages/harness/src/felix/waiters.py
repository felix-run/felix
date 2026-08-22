"""Cross-process waiters (Redis list BLPOP) with in-process fallback."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

logger = logging.getLogger("felix.waiters")

_PREFIX = "felix:waiter:"
_local: dict[str, asyncio.Future[str]] = {}
_lock = asyncio.Lock()
_redis: Any | None = None
_redis_loop: int | None = None
_redis_failed = False


async def _get_redis() -> Any | None:
    """Return a Redis client bound to the current event loop, or None."""
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
        logger.debug("waiter redis unavailable; using in-process", exc_info=True)
        _redis_failed = True
        return None


def _key(name: str) -> str:
    return f"{_PREFIX}{name}"


async def wait(name: str, *, timeout: float) -> dict[str, Any] | None:
    """Block until ``signal(name, payload)`` or timeout. Returns payload or None."""
    rkey = _key(name)
    client = await _get_redis()
    if client is not None:
        try:
            item = await client.blpop(rkey, timeout=max(1, int(timeout)))
            if not item:
                return None
            _, raw = item
            return json.loads(raw)
        except Exception:
            logger.debug("waiter redis blpop failed", exc_info=True)

    async with _lock:
        fut = _local.get(name)
        if fut is None:
            fut = asyncio.get_running_loop().create_future()
            _local[name] = fut
        elif fut.done():
            raw = fut.result()
            _local.pop(name, None)
            return json.loads(raw)
    try:
        raw = await asyncio.wait_for(fut, timeout=timeout)
        return json.loads(raw)
    except TimeoutError:
        return None
    finally:
        async with _lock:
            if _local.get(name) is fut:
                _local.pop(name, None)


async def signal(name: str, payload: dict[str, Any]) -> bool:
    """Deliver a payload to a waiting ``wait(name)`` caller."""
    rkey = _key(name)
    raw = json.dumps(payload, default=str)
    client = await _get_redis()
    if client is not None:
        try:
            await client.rpush(rkey, raw)
            await client.expire(rkey, 3600)
            return True
        except Exception:
            logger.debug("waiter redis rpush failed", exc_info=True)

    async with _lock:
        fut = _local.get(name)
        if fut is None:
            fut = asyncio.get_running_loop().create_future()
            fut.set_result(raw)
            _local[name] = fut
            return True
        if fut.done():
            return False
        fut.set_result(raw)
        return True


__all__ = ["signal", "wait"]
