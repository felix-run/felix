"""Wake a reader when a thread gets new events, instead of asking every second.

`chat_stream_resume` polls `get_events` per connected client. Query volume there grows
with *connected users* rather than with turns, which is the line that crosses first: a
thousand reattached tabs is a sustained query rate that learns nothing, against a pool
whose ceiling is `WORKERS x (POOL_SIZE + MAX_OVERFLOW)`.

Two properties make this safe to rely on.

**The notification is a hint, never the source of truth.** Redis pub/sub is at-most-once
and drops messages on a reconnect, so a lost wake must cost latency and nothing else.
Every wake — whether from a notification or a timeout — runs the same `get_events` query
it ran before. That is what lets the poll ceiling relax rather than disappear.

**One subscriber connection per process, not per stream.** A connection per SSE stream
would move the ceiling from Postgres to Redis and solve nothing. Channels are
subscribed and unsubscribed as readers come and go, ref-counted, on a single shared
connection with one reader task pumping it.

Without Redis this degrades to the in-process layer, which is complete for a
single-replica deployment — including every `memory://` test — because the writer and
the reader are the same process. The poll remains underneath either way.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("felix.session.notify")

_PREFIX = "felix:thread:"

# Local waiters, keyed by channel. Present whether or not Redis is reachable: a writer
# in this process wakes a reader in this process directly, which is the whole mechanism
# for a single-replica deployment.
_waiters: dict[str, set[asyncio.Event]] = {}

# How long to stay on the polling path after a failed connection attempt.
#
# Not a permanent latch: a Redis blip would otherwise degrade a worker to polling for
# the life of the process, and the whole point of this module is that the poll is the
# safety net rather than the mechanism. Long enough that a hard-down Redis costs one
# attempt per interval rather than one per wait.
_RETRY_AFTER_SECONDS = 30.0

_redis: Any | None = None
_redis_loop: int | None = None
_redis_failed_until: float = 0.0
_pubsub: Any | None = None
_pump: asyncio.Task[None] | None = None
_subscribed: dict[str, int] = {}


def _channel(tenant_id: str, thread_id: str) -> str:
    return f"{_PREFIX}{tenant_id}:{thread_id}"


@dataclass(frozen=True, slots=True)
class Wake:
    """Why a wait returned, and whether the next one can afford to wait longer.

    `by_notification` is not merely informational: it tells the caller a wake is
    actually being delivered, so a longer poll interval is a safety net rather than the
    only path. When Redis drops, this goes False on the next wait and the caller
    tightens its interval again without anything having to notice.
    """

    woken: bool
    by_notification: bool


async def _get_redis() -> Any | None:
    global _redis, _redis_loop, _redis_failed_until
    loop_id = id(asyncio.get_running_loop())
    if _redis is not None and _redis_loop != loop_id:
        await _teardown()
    if time.monotonic() < _redis_failed_until:
        return None
    if _redis is not None:
        return _redis
    try:
        from felix.config import get_settings

        url = (getattr(get_settings(), "redis_url", "") or "").strip()
        if not url:
            # Configuration, not a blip: nothing to retry, so back off for a long time
            # rather than re-reading settings on every wait.
            _redis_failed_until = time.monotonic() + 3600.0
            return None
        import redis.asyncio as redis

        client = redis.from_url(url, decode_responses=True, socket_connect_timeout=1.0, socket_timeout=2.0)
        await client.ping()
        _redis, _redis_loop = client, loop_id
        return _redis
    except Exception:
        logger.debug("thread notifications unavailable; polling only", exc_info=True)
        _redis_failed_until = time.monotonic() + _RETRY_AFTER_SECONDS
        return None


async def _teardown() -> None:
    global _redis, _redis_loop, _redis_failed_until, _pubsub, _pump
    if _pump is not None:
        _pump.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await _pump
    for obj in (_pubsub, _redis):
        if obj is not None:
            with contextlib.suppress(Exception):
                await obj.aclose()
    _redis = _redis_loop = _pubsub = _pump = None
    _redis_failed_until = 0.0
    _subscribed.clear()


async def _pump_messages(pubsub: Any) -> None:
    """Fan Redis messages out to local waiters.

    Errors end the pump rather than spinning: the next `wait_for_events` rebuilds it,
    and until then the poll underneath keeps the stream correct.
    """
    try:
        async for message in pubsub.listen():
            if message.get("type") != "message":
                continue
            _wake_local(str(message.get("channel") or ""))
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug("thread notification pump stopped; polling only", exc_info=True)


def _wake_local(channel: str) -> None:
    """Set every local waiter on this channel.

    No lock. Each step here is a plain dict or set operation with no `await` between
    read and write, so asyncio cannot interleave another task into the middle of one.
    A module-level `asyncio.Lock` would be worse than useless: its waiters are bound to
    the loop that created them, so a process using more than one event loop -- every
    test session does -- can leave it held by a loop that no longer exists.
    """
    for event in list(_waiters.get(channel) or ()):
        event.set()


async def _ensure_subscribed(channel: str) -> bool:
    """Subscribe this process to `channel`, sharing one connection. True if subscribed."""
    global _pubsub, _pump
    client = await _get_redis()
    if client is None:
        return False
    try:
        if _pubsub is None:
            _pubsub = client.pubsub(ignore_subscribe_messages=True)
        if _subscribed.get(channel, 0) == 0:
            await _pubsub.subscribe(channel)
        _subscribed[channel] = _subscribed.get(channel, 0) + 1
        if _pump is None or _pump.done():
            _pump = asyncio.create_task(_pump_messages(_pubsub))
        return True
    except Exception:
        logger.debug("thread notification subscribe failed; polling only", exc_info=True)
        return False


async def _release(channel: str, subscribed: bool) -> None:
    if not subscribed:
        return
    remaining = _subscribed.get(channel, 0) - 1
    if remaining > 0:
        _subscribed[channel] = remaining
        return
    _subscribed.pop(channel, None)
    if _pubsub is not None:
        with contextlib.suppress(Exception):
            await _pubsub.unsubscribe(channel)


async def wait_for_events(tenant_id: str, thread_id: str, *, timeout: float) -> Wake:
    """Wait until this thread is appended to, or `timeout` elapses.

    Always returns rather than raising: a caller polls on the way out either way, so a
    notification layer that is down must look like a slow one.
    """
    channel = _channel(tenant_id, thread_id)
    event = asyncio.Event()
    _waiters.setdefault(channel, set()).add(event)
    # Registration is outside the `try`, so the `try` has to start immediately after
    # it: `_ensure_subscribed` awaits, and a task cancelled during that await would
    # otherwise skip the `finally` and leave its waiter registered forever. One leaked
    # waiter per cancelled stream, in the component added to help at scale.
    subscribed = False
    try:
        subscribed = await _ensure_subscribed(channel)
        await asyncio.wait_for(event.wait(), timeout=timeout)
        return Wake(woken=True, by_notification=subscribed)
    except TimeoutError:
        return Wake(woken=False, by_notification=subscribed)
    finally:
        holders = _waiters.get(channel)
        if holders is not None:
            holders.discard(event)
            if not holders:
                _waiters.pop(channel, None)
        await _release(channel, subscribed)


async def notify_appended(tenant_id: str, thread_id: str) -> None:
    """Announce that a thread has new events. Best effort, and never raises.

    Called from the append path, so it covers every writer -- the agent loop, steering,
    tool results, and the management API -- rather than only the one a route happens to
    know about. A failure here must not fail the append that succeeded.
    """
    channel = _channel(tenant_id, thread_id)
    _wake_local(channel)
    try:
        # `_get_redis` caches its client and returns None once it has failed, so this
        # costs one connection attempt per process rather than one per append.
        client = await _get_redis()
        if client is not None:
            await client.publish(channel, "1")
    except Exception:
        logger.debug("thread notification publish failed", exc_info=True)


async def reset_notifications() -> None:
    """Drop every connection and waiter. For tests and process shutdown."""
    _waiters.clear()
    await _teardown()


__all__ = ["Wake", "notify_appended", "reset_notifications", "wait_for_events"]
