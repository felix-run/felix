"""Eviction cost must not scale with the number of keys being tracked.

Keys are per-IP, so key count is attacker-influenced: a spray of source addresses
inflates whatever work the limiter does per request. The old sweep ran `max(v)` across
every tracked key and the overflow branch then `sorted()` them with the same `max()` as
its key function — so the defensive component's cost grew with the attack it exists to
absorb.

Measured on this checkout, worst single hit with the eviction gate open:

    keys     before      after
    10,000   273.6 µs    1.0 µs
    20,000   558.1 µs    0.8 µs
    50,000  1412.7 µs    6.7 µs

and the steady-state hot key with a full 120-entry bucket went 13.92 µs → 0.37 µs,
because the old implementation rebuilt the whole timestamp list on every request.

The trade is real and small: a fresh-key request now pays ~0.15 µs more, because
expiry is amortised into every call instead of batched into one. That is the price of
the stall going away.
"""

from __future__ import annotations

import ast
import inspect
import time

import pytest
from felix.security import rate_limit as rl_module
from felix.security.rate_limit import MAX_TRACKED_KEYS, InMemoryRateLimiter


async def _spray(limiter: InMemoryRateLimiter, n: int, *, limit: int = 120) -> None:
    for i in range(n):
        await limiter.hit(f"ip:10.0.{i // 256}.{i % 256}", limit=limit, window_seconds=60)


# --- the semantics must not have moved ----------------------------------------------


@pytest.mark.asyncio
async def test_a_window_still_allows_exactly_the_limit() -> None:
    rl = InMemoryRateLimiter()
    allowed = [await rl.hit("ip:a", limit=3, window_seconds=60) for _ in range(5)]
    assert allowed == [True, True, True, False, False]


@pytest.mark.asyncio
async def test_a_rejected_request_does_not_extend_the_window() -> None:
    """The rejected hit must not be recorded, or a client at the limit could never
    recover: each rejection would push the window forward."""
    rl = InMemoryRateLimiter()
    for _ in range(3):
        await rl.hit("ip:a", limit=3, window_seconds=60)
    before = list(rl._windows["ip:a"])
    assert await rl.hit("ip:a", limit=3, window_seconds=60) is False
    assert list(rl._windows["ip:a"]) == before


@pytest.mark.asyncio
async def test_keys_are_independent() -> None:
    rl = InMemoryRateLimiter()
    for _ in range(3):
        await rl.hit("ip:a", limit=3, window_seconds=60)
    assert await rl.hit("ip:a", limit=3, window_seconds=60) is False
    assert await rl.hit("ip:b", limit=3, window_seconds=60) is True


@pytest.mark.asyncio
async def test_a_stale_bucket_frees_the_allowance() -> None:
    """window_seconds=0 puts every recorded timestamp at or before the cutoff."""
    rl = InMemoryRateLimiter()
    for _ in range(10):
        assert await rl.hit("ip:a", limit=1, window_seconds=0) is True


# --- the properties the change is about ----------------------------------------------


@pytest.mark.asyncio
async def test_expired_keys_are_reclaimed_without_a_sweep() -> None:
    rl = InMemoryRateLimiter()
    await _spray(rl, 200)
    assert len(rl._windows) == 200
    # Every existing key is now expired; each subsequent hit reclaims a bounded number.
    for _ in range(200):
        await rl.hit("ip:fresh", limit=120, window_seconds=0)
    assert len(rl._windows) <= 2, f"stale keys were never reclaimed: {len(rl._windows)}"


@pytest.mark.asyncio
async def test_the_key_ceiling_holds() -> None:
    """Without a bound, an IP spray is a memory-exhaustion DoS in the component whose
    job is to prevent one."""
    rl = InMemoryRateLimiter()
    original = rl_module.MAX_TRACKED_KEYS
    rl_module.MAX_TRACKED_KEYS = 50
    try:
        await _spray(rl, 500)
        assert len(rl._windows) <= 50
    finally:
        rl_module.MAX_TRACKED_KEYS = original
    assert MAX_TRACKED_KEYS == 50_000, "the shipped ceiling"


@pytest.mark.asyncio
async def test_eviction_examines_a_bounded_number_of_keys() -> None:
    """Structural, so it holds regardless of machine speed.

    The request path must not iterate every tracked key. `sorted(self._windows, ...)`
    and `{k: v for k, v in self._windows.items() ...}` are the two shapes that
    reintroduce the stall.
    """
    src = inspect.getsource(InMemoryRateLimiter)
    tree = ast.parse(src.lstrip())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "sorted":
            raise AssertionError(f"sorted() on the request path at line {node.lineno}")
        if isinstance(node, ast.Attribute) and node.attr in {"items", "values", "keys"}:
            raise AssertionError(f"line {node.lineno} walks the whole key set; eviction must stay bounded")


@pytest.mark.asyncio
async def test_a_hit_stays_fast_with_fifty_thousand_keys_tracked() -> None:
    """A latency property deserves a latency assertion.

    The bound is deliberately loose — measured at 6.7 µs here against 1412.7 µs before,
    so 500 µs leaves ~75x headroom for a slow CI box while still failing outright if the
    full sweep ever comes back.
    """
    rl = InMemoryRateLimiter()
    await _spray(rl, 50_000)
    # `window_seconds=0` makes every tracked key expired as of this instant, which is
    # what opens the old implementation's sweep gate. Without it the probe measures
    # only the fast path and the regression it exists to catch slips through -- the
    # first version of this test passed against the very code it was written against.
    start = time.perf_counter()
    await rl.hit("ip:probe", limit=120, window_seconds=0)
    elapsed_us = (time.perf_counter() - start) * 1e6
    assert elapsed_us < 500, f"one hit took {elapsed_us:.0f} µs with 50k keys tracked"


def test_the_redis_pipeline_is_not_transactional() -> None:
    """MULTI/EXEC buys atomicity INCR + EXPIRE does not need — the `nx` on EXPIRE
    already makes it idempotent — and pays two extra round trips per request."""
    calls: list[dict] = []

    class _Pipe:
        def incr(self, *a, **k) -> None: ...
        def expire(self, *a, **k) -> None: ...
        async def execute(self) -> list[int]:
            return [1]

    class _Redis:
        def pipeline(self, **kwargs):
            calls.append(kwargs)
            return _Pipe()

    import asyncio

    from felix.security.rate_limit import RedisRateLimiter

    asyncio.run(RedisRateLimiter(redis=_Redis()).hit("k", limit=10, window_seconds=60))
    assert calls == [{"transaction": False}], f"pipeline called with {calls}"
