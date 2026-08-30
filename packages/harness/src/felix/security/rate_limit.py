"""In-process / Redis-backed per-tenant rate limiting."""

from __future__ import annotations

import logging
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger("felix.security.rate_limit")


class RateLimiterBackend(Protocol):
    async def hit(self, key: str, *, limit: int, window_seconds: int) -> bool:
        """Return True if the request is allowed."""
        ...


# Cap on distinct keys held in memory. Keys are per-IP, so without a bound a spray of
# source addresses grows the dict forever — a memory-exhaustion DoS in the component
# whose job is to prevent DoS.
MAX_TRACKED_KEYS = 50_000

# Keys examined for expiry per request. Eviction is incremental rather than a periodic
# full sweep: the sweep ran `max(v)` over every tracked key *inside a request*, which at
# 20,000 keys stalled the whole worker for ~1 ms — and key count is attacker-influenced,
# since keys are per-IP. A spray of source addresses inflated the work the defensive
# component did on every request it served. Four is enough to outpace one new key per
# request while staying constant-cost.
EVICT_PER_HIT = 4


@dataclass
class InMemoryRateLimiter:
    """Sliding-ish fixed window for tests and single-process deploys.

    Ordered by last hit, so the front of `_windows` is always the least-recently-seen
    key — which is exactly the key whose window elapsed longest ago. That makes expiry
    a look at the front rather than a scan, and it makes the ceiling a `popitem` rather
    than a sort.
    """

    _windows: OrderedDict[str, deque[float]] = field(default_factory=OrderedDict)

    async def hit(self, key: str, *, limit: int, window_seconds: int) -> bool:
        now = time.monotonic()
        cutoff = now - window_seconds
        self._evict_some(cutoff)

        # `.get`, not `[]`: the old defaultdict inserted an entry for every key it was
        # merely asked about.
        bucket = self._windows.get(key)
        if bucket is None:
            bucket = deque()
            self._windows[key] = bucket
        else:
            self._windows.move_to_end(key)

        # Timestamps are appended in order, so the deque is sorted and dropping from
        # the left is amortised O(1) -- where the old comprehension rebuilt the whole
        # list on every request.
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()

        if len(bucket) >= limit:
            return False
        bucket.append(now)
        self._enforce_ceiling()
        return True

    def _evict_some(self, cutoff: float) -> None:
        """Expire a bounded number of the oldest keys.

        Bounded, so the cost per request is constant no matter how many keys are
        tracked. Correct, because insertion order is last-hit order: once the front
        key is still live, every key behind it is too.
        """
        for _ in range(EVICT_PER_HIT):
            if not self._windows:
                return
            key = next(iter(self._windows))
            bucket = self._windows[key]
            if bucket and bucket[-1] > cutoff:
                return
            del self._windows[key]

    def _enforce_ceiling(self) -> None:
        """A hard cap, in case arrivals outpace expiry.

        Keys are per-IP, so without a bound a spray of source addresses grows the dict
        forever — a memory-exhaustion DoS in the component whose job is to prevent one.
        """
        while len(self._windows) > MAX_TRACKED_KEYS:
            self._windows.popitem(last=False)


@dataclass
class RedisRateLimiter:
    """Fixed-window counter via Redis INCR + EXPIRE."""

    redis: Any  # redis.asyncio.Redis
    prefix: str = "felix:rl:"

    async def hit(self, key: str, *, limit: int, window_seconds: int) -> bool:
        rkey = f"{self.prefix}{key}"
        # INCR then EXPIRE is not atomic: a crash between them leaves a key with no TTL,
        # and that principal is then rate-limited permanently. A pipeline applies both.
        try:
            # `transaction=False`: the default wraps INCR + EXPIRE in MULTI/EXEC, which
            # buys atomicity this does not need — the two commands are independent and
            # the `nx` on EXPIRE already makes it idempotent — and pays two extra round
            # trips per request for it.
            pipe = self.redis.pipeline(transaction=False)
            pipe.incr(rkey)
            pipe.expire(rkey, window_seconds, nx=True)
            results = await pipe.execute()
            count = int(results[0])
        except TypeError:
            # redis-py below 4.6 has no `nx` on EXPIRE; fall back to the two-step form.
            count = int(await self.redis.incr(rkey))
            if count == 1:
                await self.redis.expire(rkey, window_seconds)
        return count <= limit


# /metrics is deliberately NOT skipped: it is a scrape target with unbounded label
# cardinality, so an unauthenticated caller polling it is a real amplification path.
SKIP_EXACT = frozenset({"/health", "/docs", "/openapi.json", "/redoc"})
# `/docs/` was here for Swagger UI's /docs/oauth2-redirect, which went with it; the
# Scalar reference is the single exact path above.
SKIP_PREFIX = ("/.well-known/",)


def should_skip_rate_limit(path: str) -> bool:
    if path in SKIP_EXACT:
        return True
    return any(path.startswith(p) for p in SKIP_PREFIX)


@dataclass
class RateLimitConfig:
    limit: int = 120
    window_seconds: int = 60
    backend: RateLimiterBackend = field(default_factory=InMemoryRateLimiter)


async def check_rate_limit(
    key: str,
    config: RateLimitConfig,
) -> bool:
    """Return True when the request may proceed."""
    return await config.backend.hit(key, limit=config.limit, window_seconds=config.window_seconds)


@dataclass
class ResilientRateLimiter:
    """Redis-backed limiting that degrades to in-process rather than failing requests.

    Two bad options to avoid. Failing the request when Redis is unreachable turns a cache
    blip into a full outage — `Redis.from_url` connects lazily, so the error surfaces on
    the request path. Skipping the limit entirely removes the control exactly when a
    dependency is already struggling. Degrading to a per-process window keeps a limit in
    force, just a looser one, and says so.
    """

    primary: RateLimiterBackend
    fallback: RateLimiterBackend = field(default_factory=InMemoryRateLimiter)
    _degraded: bool = False

    async def hit(self, key: str, *, limit: int, window_seconds: int) -> bool:
        try:
            allowed = await self.primary.hit(key, limit=limit, window_seconds=window_seconds)
        except Exception:
            if not self._degraded:
                self._degraded = True
                logger.error(
                    "redis rate limiter unreachable; degrading to per-process limits "
                    "(the effective ceiling is now limit x replicas)",
                    exc_info=True,
                )
            return await self.fallback.hit(key, limit=limit, window_seconds=window_seconds)
        if self._degraded:
            self._degraded = False
            logger.info("redis rate limiter recovered")
        return allowed


def build_rate_limit_config(settings: Any) -> RateLimitConfig:
    """Rate-limit config from settings, using Redis when one is configured.

    `RateLimitConfig()` was constructed with no arguments, so the limit was hardcoded at
    120/60s and the backend was always in-memory — `RedisRateLimiter` existed but was
    never wired, meaning limits were per-process and the effective ceiling was
    N_replicas x 120.
    """
    backend: RateLimiterBackend = InMemoryRateLimiter()
    url = (getattr(settings, "redis_url", "") or "").strip()
    if url:
        try:
            from redis.asyncio import Redis

            backend = ResilientRateLimiter(primary=RedisRateLimiter(redis=Redis.from_url(url)))
        except Exception:
            logger.warning("redis rate limiter unavailable; using in-process limits", exc_info=True)
    return RateLimitConfig(
        limit=int(getattr(settings, "rate_limit", 120) or 120),
        window_seconds=int(getattr(settings, "rate_limit_window_seconds", 60) or 60),
        backend=backend,
    )


def client_key(request: Any, settings: Any) -> str:
    """Rate-limit key for one request.

    Keyed by client address, not tenant: this middleware now runs *outside* auth so that
    failed authentication is throttled, and at that point there is no principal. The
    previous per-tenant key also meant that under `auth_mode=none` every caller shared
    one `tenant:default` bucket, so a single client could 429 the whole deployment.
    """
    header = (getattr(settings, "trusted_client_ip_header", "") or "").strip().lower()
    if header:
        raw = request.headers.get(header) or ""
        # X-Forwarded-For is a list; the first entry is the origin client.
        candidate = raw.split(",")[0].strip()
        if candidate:
            return f"ip:{candidate}"
    client = getattr(request, "client", None)
    host = getattr(client, "host", None) if client is not None else None
    return f"ip:{host or 'unknown'}"


__all__ = [
    "InMemoryRateLimiter",
    "RateLimitConfig",
    "RateLimiterBackend",
    "RedisRateLimiter",
    "ResilientRateLimiter",
    "build_rate_limit_config",
    "check_rate_limit",
    "client_key",
    "should_skip_rate_limit",
]
