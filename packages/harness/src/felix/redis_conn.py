"""One lazily-connected Redis client, shared by everything that needs one.

Three modules — `session/notify.py`, `steer.py`, `waiters.py` — each grew their own
`_get_redis` with the same shape and the same two defects, because the shape was copied
before it was right:

**A failed connection latched forever.** `_redis_failed = True` was never cleared except
on a loop change, so one blip put the process on its in-process fallback for good. For
notifications that costs latency. For steer it costs *correctness*: `enqueue` writes to a
local queue, returns `{"queued": "steer"}`, and the replica actually running the turn
never sees it. A user types "stop", gets a 200, and the agent keeps going.

**`id(asyncio.get_running_loop())` is not a loop identity.** CPython reuses freed
addresses, so a new loop allocated where a closed one lived compares equal, the teardown
is skipped, and the module reuses a client bound to a dead loop.

Both are fixed here once. A failure backs off for `retry_after_seconds` and then tries
again — the fallback is a safety net, not a destination — and the loop is held by weak
reference, so a dead one compares unequal by construction.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import weakref
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger("felix.redis")

#: How long to stay on the fallback path after a failed connection attempt. Long enough
#: that a hard-down Redis costs one attempt per interval rather than one per call.
RETRY_AFTER_SECONDS = 30.0

#: How long to wait before re-reading settings that named no Redis at all. That is
#: configuration rather than a blip, so there is nothing to retry soon.
UNCONFIGURED_RETRY_SECONDS = 3600.0


class RedisConnection:
    """A process-wide Redis client for one subsystem.

    Not a pool: `redis.asyncio` clients are already pooled internally. This owns the
    decision of *when* to have one — the connect, the back-off, and noticing that the
    event loop underneath it has been replaced.
    """

    __slots__ = (
        "_client",
        "_connecting",
        "_consequence",
        "_failed_until",
        "_label",
        "_loop",
        "_on_reset",
        "_retry_after",
        "_warned",
    )

    def __init__(
        self,
        label: str,
        *,
        retry_after_seconds: float = RETRY_AFTER_SECONDS,
        on_reset: Callable[[], Awaitable[None]] | None = None,
        fallback_consequence: str = "",
    ) -> None:
        self._label = label
        #: What the subsystem loses on the in-process fallback, in its own words, for the
        #: one warning this helper logs when a configured Redis is unreachable. The helper
        #: knows only that the fallback does not cross processes; what that costs — a
        #: "stop" that went nowhere, an approval that never arrives — is the caller's.
        self._consequence = fallback_consequence
        self._retry_after = retry_after_seconds
        #: Called before the client is dropped, for subsystems holding state derived
        #: from it — a pub/sub connection and its reader task, say — which is not
        #: something this class can clean up on their behalf.
        self._on_reset = on_reset
        self._client: Any | None = None
        self._loop: weakref.ref[asyncio.AbstractEventLoop] | None = None
        self._connecting: asyncio.Future[None] | None = None
        self._failed_until = 0.0
        self._warned = False

    async def get(self) -> Any | None:
        """The client, connecting if needed. `None` means use your fallback."""
        loop = asyncio.get_running_loop()
        if self._client is not None and (self._loop is None or self._loop() is not loop):
            await self.aclose()
        if time.monotonic() < self._failed_until:
            return None
        if self._client is not None:
            return self._client
        if self._connecting is not None:
            if not self._connecting.done() and self._connecting.get_loop() is loop:
                # Another task is mid-connect. Waiting on it rather than building a
                # second client is what stops concurrent cold starts from opening
                # several and closing none of the losers.
                with contextlib.suppress(Exception):
                    await self._connecting
                return self._client
            # A guard with no flight behind it — a connect whose loop closed before it
            # could finish. Short-circuiting on it would turn a single-flight guard into
            # the permanent latch this class exists to remove.
            self._connecting = None

        self._connecting = fut = loop.create_future()
        try:
            from felix.config import get_settings

            url = (getattr(get_settings(), "redis_url", "") or "").strip()
            if not url:
                self._failed_until = time.monotonic() + UNCONFIGURED_RETRY_SECONDS
                return None

            import redis.asyncio as redis

            client = redis.from_url(
                url, decode_responses=True, socket_connect_timeout=1.0, socket_timeout=2.0
            )
            await client.ping()
            self._client, self._loop = client, weakref.ref(loop)
            if self._warned:
                self._warned = False
                logger.warning("%s: redis reachable again; cross-process delivery restored", self._label)
            return self._client
        except Exception:
            if not self._warned:
                # Once, at warning: the fallback is process-local, so an approval decided on
                # the API never reaches a fiber on the worker. A debug line made a configured
                # Redis that was down look exactly like one that was working.
                self._warned = True
                logger.warning(
                    "%s: redis at FELIX_REDIS_URL unreachable; using the in-process fallback, which "
                    "does not cross processes%s — retrying in %.0fs",
                    self._label,
                    f" ({self._consequence})" if self._consequence else "",
                    self._retry_after,
                    exc_info=True,
                )
            else:
                logger.debug("%s: redis still unavailable, using the fallback", self._label)
            self._failed_until = time.monotonic() + self._retry_after
            return None
        finally:
            # `fut`, not `self._connecting`: whoever created a future resolves it, or a
            # waiter blocks forever on one nobody owns any more.
            if not fut.done():
                fut.set_result(None)
            if self._connecting is fut:
                self._connecting = None

    async def report_failure(self) -> None:
        """A command on the client failed: drop it so the next `get()` reconnects.

        Without this a Redis that dies *after* the client was established is never noticed
        here — `get()` hands back the cached client forever, every command fails into the
        caller's fallback, and the warning above never fires because nothing reconnects.
        Delegates to `aclose`, so `on_reset` runs and state derived from the dead client
        (a pub/sub, say) goes with it.
        """
        await self.aclose()

    async def fallback(self, what: str) -> None:
        """The one line a command's `except` needs: drop the client, log what fell back."""
        await self.report_failure()
        logger.debug("%s: %s failed; using the fallback", self._label, what, exc_info=True)

    async def aclose(self) -> None:
        """Drop the client and any state derived from it. Safe to call at any time."""
        if self._on_reset is not None:
            with contextlib.suppress(Exception):
                await self._on_reset()
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.aclose()
        self._client = None
        self._loop = None
        self._connecting = None
        self._failed_until = 0.0


__all__ = ["RETRY_AFTER_SECONDS", "UNCONFIGURED_RETRY_SECONDS", "RedisConnection"]
