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
connection with one reader task pumping it. "As readers come and go" is what
`thread_watch` is for -- an earlier version said this while subscribing and
unsubscribing per *wait*, which is once per poll interval per stream.

Without Redis this degrades to the in-process layer, which is complete for a
single-replica deployment — including every `memory://` test — because the writer and
the reader are the same process. The poll remains underneath either way.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import weakref
from collections.abc import AsyncIterator
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
_redis_loop: weakref.ref[asyncio.AbstractEventLoop] | None = None
_connecting: asyncio.Future[None] | None = None
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
    global _redis, _redis_loop, _redis_failed_until, _connecting
    loop = asyncio.get_running_loop()
    if _redis is not None and (_redis_loop is None or _redis_loop() is not loop):
        # A weak reference rather than `id()`: CPython reuses freed addresses, so a new
        # loop allocated where a closed one lived compares equal by id, the teardown is
        # skipped, and the module goes on using a connection bound to a dead loop. A
        # dead loop's weakref reads as None, which compares unequal by construction.
        await _teardown()
    if time.monotonic() < _redis_failed_until:
        return None
    if _redis is not None:
        return _redis
    if _connecting is not None:
        if not _connecting.done() and _connecting.get_loop() is loop:
            # Another task is mid-connect. Awaiting its future rather than building a
            # second client is what stops concurrent cold starts from each opening a
            # connection and orphaning all but the last -- nothing ever closed the losers.
            with contextlib.suppress(Exception):
                await _connecting
            return _redis
        # A guard with no flight behind it: a connect whose loop closed before its
        # `finally` could run. Short-circuiting on it turns a single-flight guard into a
        # permanent latch -- every later call returns `_redis` without attempting a
        # connect, which is the indefinite degradation `_RETRY_AFTER_SECONDS` exists to
        # prevent. Discard it and connect.
        _connecting = None
    _connecting = fut = loop.create_future()
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
        _redis, _redis_loop = client, weakref.ref(loop)
        return _redis
    except Exception:
        logger.debug("thread notifications unavailable; polling only", exc_info=True)
        _redis_failed_until = time.monotonic() + _RETRY_AFTER_SECONDS
        return None
    finally:
        # `fut`, not the global: whoever created a future must be the one to resolve it,
        # or a waiter blocks forever on a future nobody owns any more. The global can be
        # cleared under us -- `_teardown` does exactly that.
        if not fut.done():
            fut.set_result(None)
        if _connecting is fut:
            _connecting = None


async def _teardown() -> None:
    global _redis, _redis_loop, _redis_failed_until, _pubsub, _pump, _connecting
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
    # The in-flight guard too, or `reset_notifications` does not do what it says: a
    # connect still pending here leaves a guard nothing will ever clear.
    _connecting = None
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

    `Event.set` schedules callbacks through the loop that created the event, so a waiter
    left behind by a loop that has since closed raises here rather than being merely
    useless. Discarding it is the fix: this function is documented never to raise, and a
    waiter whose loop is gone can never be woken again anyway.
    """
    holders = _waiters.get(channel)
    if not holders:
        return
    for event in list(holders):
        try:
            event.set()
        except RuntimeError:
            holders.discard(event)
    if not holders:
        _waiters.pop(channel, None)


async def _ensure_subscribed(channel: str) -> bool:
    """Subscribe this process to `channel`, sharing one connection. True if subscribed."""
    global _pubsub, _pump
    client = await _get_redis()
    if client is None:
        return False
    # Claim the refcount slot *before* the await, not after.
    #
    # The read-then-await-then-write shape this replaces let a second reader observe a
    # count of 0 while the first was still inside SUBSCRIBE. The first would then finish
    # its wait and release 1 -> 0, putting UNSUBSCRIBE last on the wire, and the second
    # was left holding a refcount over a connection subscribed to nothing -- while
    # reporting `by_notification=True`, which tells the caller it is safe to stretch its
    # poll interval to a minute. A stream that believes it is being woken and is not.
    first = _subscribed.get(channel, 0) == 0
    _subscribed[channel] = _subscribed.get(channel, 0) + 1
    try:
        if _pubsub is None:
            _pubsub = client.pubsub(ignore_subscribe_messages=True)
        if first:
            await _pubsub.subscribe(channel)
        if _pump is None or _pump.done():
            _pump = asyncio.create_task(_pump_messages(_pubsub))
        return True
    except Exception:
        logger.debug("thread notification subscribe failed; polling only", exc_info=True)
        remaining = _subscribed.get(channel, 0) - 1
        if remaining > 0:
            _subscribed[channel] = remaining
        else:
            _subscribed.pop(channel, None)
        return False


async def _release(channel: str) -> None:
    """Drop one hold on `channel`, unsubscribing when the last one goes."""
    remaining = _subscribed.get(channel, 0) - 1
    if remaining >= 1:
        _subscribed[channel] = remaining
        return
    # `remaining` goes negative when `_teardown` cleared `_subscribed` while readers
    # still held entries in it. Treating that as "nothing holds this any more" is right
    # either way; the old `> 0` test let a negative fall through to the unsubscribe
    # below, dropping a channel other live readers depended on.
    _subscribed.pop(channel, None)
    if _pubsub is not None:
        with contextlib.suppress(Exception):
            await _pubsub.unsubscribe(channel)


class ThreadWatch:
    """A subscription held for as long as a reader is reading.

    The unit here is the reader, not the wait. Subscribing per wait meant a SUBSCRIBE
    and an UNSUBSCRIBE round trip on every poll iteration of every stream, serialized
    through the one shared connection this module exists to conserve -- and it reopened
    the refcount race in `_ensure_subscribed` once per second per stream.

    Holding the `asyncio.Event` across waits also closes a gap the per-wait version had:
    an append landing between two waits used to be missed, because the event it set was
    discarded with the wait that owned it. Now it stays set and the next `wait` returns
    at once.
    """

    __slots__ = ("_channel", "_event", "delivering")

    def __init__(self, channel: str, event: asyncio.Event, *, delivering: bool) -> None:
        self._channel = channel
        self._event = event
        #: Whether a cross-process channel is actually behind this watch.
        self.delivering = delivering

    async def wait(self, *, timeout: float) -> Wake:
        """Wait for the next append, or `timeout`. Never raises on the timeout path."""
        try:
            await asyncio.wait_for(self._event.wait(), timeout=timeout)
        except TimeoutError:
            return Wake(woken=False, by_notification=self.delivering)
        self._event.clear()
        return Wake(woken=True, by_notification=self.delivering)


@contextlib.asynccontextmanager
async def thread_watch(tenant_id: str, thread_id: str) -> AsyncIterator[ThreadWatch]:
    """Watch a thread for the life of a reader.

    Registration happens before the `try`, so the `try` starts immediately after it:
    `_ensure_subscribed` awaits, and a task cancelled during that await would otherwise
    skip the `finally` and leave its waiter registered forever. One leaked waiter per
    cancelled stream, in the component added to help at scale.
    """
    channel = _channel(tenant_id, thread_id)
    event = asyncio.Event()
    _waiters.setdefault(channel, set()).add(event)
    subscribed = False
    try:
        subscribed = await _ensure_subscribed(channel)
        yield ThreadWatch(channel, event, delivering=subscribed)
    finally:
        holders = _waiters.get(channel)
        if holders is not None:
            holders.discard(event)
            if not holders:
                _waiters.pop(channel, None)
        if subscribed:
            await _release(channel)


async def wait_for_events(tenant_id: str, thread_id: str, *, timeout: float) -> Wake:
    """Wait once for an append to this thread, or until `timeout` elapses.

    A caller that waits repeatedly should hold a `thread_watch` instead -- this opens and
    closes a subscription around every call. Kept for callers that genuinely wait once.
    """
    async with thread_watch(tenant_id, thread_id) as watch:
        return await watch.wait(timeout=timeout)


async def notify_appended(tenant_id: str, thread_id: str) -> None:
    """Announce that a thread has new events. Best effort, and never raises.

    Called from the append path, so it covers every writer -- the agent loop, steering,
    tool results, and the management API -- rather than only the one a route happens to
    know about. A failure here must not fail the append that succeeded.
    """
    channel = _channel(tenant_id, thread_id)
    try:
        _wake_local(channel)
        # `_get_redis` caches its client and returns None once it has failed, so this
        # costs one connection attempt per process rather than one per append.
        client = await _get_redis()
        if client is not None:
            await client.publish(channel, "1")
    except Exception:
        logger.debug("thread notification publish failed", exc_info=True)


async def reset_notifications() -> None:
    """Drop every connection and waiter.

    Called from the API lifespan's shutdown `finally` and from tests. Both matter: this
    module holds a Redis client, a pubsub connection and a pump task for the life of the
    process, which is the shape of resource that lingered across Granian worker recycles
    before the lifespan started joining them.
    """
    _waiters.clear()
    await _teardown()


__all__ = [
    "ThreadWatch",
    "Wake",
    "notify_appended",
    "reset_notifications",
    "thread_watch",
    "wait_for_events",
]
