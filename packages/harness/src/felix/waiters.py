"""Cross-process waiters (Redis list BLPOP) with in-process fallback."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from felix.redis_conn import RedisConnection

logger = logging.getLogger("felix.waiters")

_PREFIX = "felix:waiter:"
_local: dict[str, asyncio.Future[str]] = {}
_lock = asyncio.Lock()
_conn = RedisConnection("waiters")


def _key(name: str) -> str:
    return f"{_PREFIX}{name}"


async def wait(name: str, *, timeout: float) -> dict[str, Any] | None:
    """Block until ``signal(name, payload)`` or timeout. Returns payload or None."""
    rkey = _key(name)
    client = await _conn.get()
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
    client = await _conn.get()
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
