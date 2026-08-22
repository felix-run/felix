"""In-process / Redis-backed per-tenant rate limiting."""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Protocol


class RateLimiterBackend(Protocol):
    async def hit(self, key: str, *, limit: int, window_seconds: int) -> bool:
        """Return True if the request is allowed."""
        ...


@dataclass
class InMemoryRateLimiter:
    """Sliding-ish fixed window for tests and single-process deploys."""

    _windows: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))

    async def hit(self, key: str, *, limit: int, window_seconds: int) -> bool:
        now = time.monotonic()
        cutoff = now - window_seconds
        bucket = self._windows[key]
        self._windows[key] = [t for t in bucket if t > cutoff]
        if len(self._windows[key]) >= limit:
            return False
        self._windows[key].append(now)
        return True


@dataclass
class RedisRateLimiter:
    """Fixed-window counter via Redis INCR + EXPIRE."""

    redis: Any  # redis.asyncio.Redis
    prefix: str = "felix:rl:"

    async def hit(self, key: str, *, limit: int, window_seconds: int) -> bool:
        rkey = f"{self.prefix}{key}"
        count = await self.redis.incr(rkey)
        if count == 1:
            await self.redis.expire(rkey, window_seconds)
        return int(count) <= limit


SKIP_EXACT = frozenset({"/health", "/docs", "/openapi.json", "/metrics", "/redoc"})
SKIP_PREFIX = ("/.well-known/", "/docs/")


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


def rate_limit_middleware(
    *,
    key_resolvers: list[Any] | None = None,
    config: RateLimitConfig | None = None,
) -> Any:
    """FastAPI http middleware — soft rate limit by tenant/IP."""
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    cfg = config or RateLimitConfig()
    resolvers = list(key_resolvers or [])

    async def middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        path = request.url.path
        if should_skip_rate_limit(path):
            return await call_next(request)
        key = "anon"
        for resolver in resolvers:
            try:
                resolved = resolver(request)
                if resolved:
                    key = str(resolved)
                    break
            except Exception:
                continue
        if key == "anon":
            from felix.context import try_get_context

            ctx = try_get_context()
            if ctx is not None:
                key = ctx.auth.tenant_id
            else:
                client = request.client.host if request.client else "unknown"
                key = f"ip:{client}"
        allowed = await check_rate_limit(key, cfg)
        if not allowed:
            return JSONResponse({"error": "rate_limited"}, status_code=429)
        return await call_next(request)

    return middleware


__all__ = [
    "InMemoryRateLimiter",
    "RateLimitConfig",
    "RateLimiterBackend",
    "RedisRateLimiter",
    "check_rate_limit",
    "rate_limit_middleware",
    "should_skip_rate_limit",
]
