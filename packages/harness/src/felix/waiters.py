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
_conn = RedisConnection(
    "waiters",
    fallback_consequence="approvals, prompts and client-tool answers decided in another process never arrive",
)


def _key(name: str) -> str:
    return f"{_PREFIX}{name}"


def waiter_name(kind: str, *parts: str) -> str:
    """Compose a waiter name from parts that cannot be confused with each other.

    A waiter name is a *key*: whoever can construct it can answer the wait. So the join has
    to be injective — two different part-tuples must never produce the same name — and
    `":".join(...)` is not, once any part may contain the separator.

    It could, and did. `client:{thread_id}:{tool_call_id}` was built with a bare f-string
    while `thread_id` legitimately carries colons (`{tenant}:{suffix}`, and
    `{tenant}:fiber:{id}` for a durable run) and `tool_call_id` arrives off the model wire
    with no charset check at all (`wire/openai_completions.py` takes `str(tc.get("id"))`).
    Two examples that collided:

        thread `acme:fiber:F123`, call `call_9`   ->  client:acme:fiber:F123:call_9
        thread `acme:fiber`,      call `F123:call_9` ->  client:acme:fiber:F123:call_9

    and `fiber` is a perfectly legal thread suffix, so the second is a thread any caller in
    that tenant can create. Answering the forged key satisfies the durable run's pending
    client tool with content the forger chose.

    Percent-encoding, `%` first so the escape cannot itself be forged. The result is never
    parsed back — injectivity is the whole requirement — but it stays readable in a log line
    and in `redis-cli --scan`, which a hash would not.
    """
    return ":".join([kind, *(p.replace("%", "%25").replace(":", "%3A") for p in parts)])


#: How long a single BLPOP may block, in seconds.
#:
#: Must stay below the client's `socket_timeout`. BLPOP blocks server-side while the
#: client sits in a socket read, so a block longer than the socket timeout raises
#: `TimeoutError` on a connection that is working perfectly — and the handler below
#: reads that as "Redis is unusable" and falls back to the in-process path.
#:
#: That is not hypothetical. With a 2 s socket timeout and the 300 s default approval
#: wait, *every* approval fell back after two seconds. The decision then went to Redis
#: while the run waited on a local future nobody would ever resolve, and the run was
#: told `denied / timeout` — after a human had clicked Approve and been told it worked.
#:
#: Slicing rather than raising the socket timeout keeps that timeout meaningful: a
#: genuinely dead connection is still detected in seconds instead of hanging for the
#: whole wait. It costs one round trip per slice, on a path where a human is thinking.
#: Latency is unaffected — BLPOP returns the moment an item is pushed.
BLOCK_SLICE_SECONDS = 1


async def wait(name: str, *, timeout: float) -> dict[str, Any] | None:
    """Block until ``signal(name, payload)`` or timeout. Returns payload or None."""
    rkey = _key(name)
    client = await _conn.get()
    if client is not None:
        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return None
                item = await client.blpop(rkey, timeout=min(BLOCK_SLICE_SECONDS, max(1, int(remaining))))
                if item:
                    _, raw = item
                    return json.loads(raw)
        except Exception:
            await _conn.fallback("waiter redis blpop")

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
            await _conn.fallback("waiter redis rpush")

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


__all__ = ["signal", "wait", "waiter_name"]
