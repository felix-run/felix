"""The connection helper three subsystems share.

`session/notify.py`, `steer.py` and `waiters.py` each grew their own `_get_redis` with
the same shape, and two of them carried the same two defects because the shape was
copied before it was right. These assertions used to live in the notification tests and
covered one of the three copies; they belong to the helper, once.

The stakes differ per caller, which is worth stating because it is why the latch matters
more than it looks. When notifications fall back, a reader polls and arrives late. When
*steer* falls back, `enqueue` writes to a process-local queue and returns
`{"queued": "steer"}` — the replica running the turn never sees it, and the user who
typed "stop" got a 200.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from felix.config import Settings
from felix.redis_conn import RedisConnection


@pytest.fixture
def unreachable(monkeypatch: pytest.MonkeyPatch):
    """Settings naming a Redis that refuses immediately, and a count of attempts."""
    from felix import config as config_mod

    attempts: list[float] = []

    def _settings() -> Settings:
        attempts.append(time.monotonic())
        # Port 1 is reserved and refuses at once, so this fails fast rather than
        # spending the connect timeout.
        return Settings(database_url="memory://x", redis_url="redis://127.0.0.1:1/0")

    monkeypatch.setattr(config_mod, "get_settings", _settings)
    return attempts


@pytest.mark.asyncio
async def test_a_failed_connection_is_retried_rather_than_latched(unreachable) -> None:
    """A blip must not put a process on its fallback for good.

    The version this replaces latched a boolean. One failed connect and that worker
    never tried again — and for notifications the poll underneath kept everything
    correct, which is exactly why nobody would notice the wake had stopped working.
    """
    conn = RedisConnection("test", retry_after_seconds=60.0)

    assert await conn.get() is None
    assert len(unreachable) == 1, "the first call should try"
    # Bounded, not merely in the future. A latch also satisfies "> now" -- that is what
    # a latch *is* -- so without this the assertions below pass against one, because
    # they reset the deadline by hand before checking the retry.
    assert time.monotonic() < conn._failed_until <= time.monotonic() + 60.0 + 5.0, (
        "the cooldown is unbounded; this is a latch wearing a deadline"
    )

    assert await conn.get() is None
    assert len(unreachable) == 1, "a call inside the cooldown should not retry"

    conn._failed_until = time.monotonic() - 1  # the cooldown elapses
    assert await conn.get() is None
    assert len(unreachable) == 2, "a call after the cooldown should retry"
    assert conn._failed_until > time.monotonic(), "the retry did not re-arm the cooldown"


@pytest.mark.asyncio
async def test_the_cooldown_is_a_cooldown() -> None:
    """The test above patches the interval, so it stays green if the shipped value were
    zero — the opposite failure, a connection attempt on every call against a Redis that
    is hard down."""
    assert RedisConnection("test")._retry_after > 0


@pytest.mark.asyncio
async def test_an_unconfigured_redis_backs_off_much_longer(monkeypatch: pytest.MonkeyPatch) -> None:
    """No URL is configuration, not a blip. Re-reading settings every call to discover
    the same absence is pure overhead on the path this is supposed to keep cheap."""
    from felix import config as config_mod

    monkeypatch.setattr(config_mod, "get_settings", lambda: Settings(database_url="memory://x", redis_url=""))
    conn = RedisConnection("test")

    assert await conn.get() is None
    assert conn._failed_until > time.monotonic() + 60, "backed off as if it were a blip"


@pytest.mark.asyncio
async def test_an_abandoned_connect_does_not_latch_the_connection_off(unreachable) -> None:
    """`_connecting` is a single-flight guard, and a guard that outlives its flight is a
    latch. The `finally` that clears it does not run if the loop closes with the connect
    still pending, which a pytest session does routinely."""
    conn = RedisConnection("test")
    stale = asyncio.get_running_loop().create_future()
    stale.set_result(None)
    conn._connecting = stale

    await conn.get()

    assert conn._connecting is None, "a spent guard survived the call that observed it"
    assert len(unreachable) == 1, "the stale guard short-circuited the connect entirely"


@pytest.mark.asyncio
async def test_closing_drops_the_in_flight_guard_too(unreachable) -> None:
    """`aclose` is what a subsystem calls to drop everything. Leaving the guard behind
    leaves the one piece of state that can latch."""
    conn = RedisConnection("test")
    conn._connecting = asyncio.get_running_loop().create_future()

    await conn.aclose()

    assert conn._connecting is None


@pytest.mark.asyncio
async def test_closing_runs_the_reset_hook_first(unreachable) -> None:
    """Callers hold state derived from the client — a pub/sub connection and its reader
    task — that this class cannot clean up for them. The hook has to run *before* the
    client goes, or that state is left pointing at a closed connection."""
    order: list[str] = []

    async def _hook() -> None:
        order.append("hook")

    conn = RedisConnection("test", on_reset=_hook)
    await conn.get()
    await conn.aclose()

    assert order == ["hook"]


@pytest.mark.asyncio
async def test_a_dead_loop_never_compares_equal_to_a_live_one() -> None:
    """`id(loop)` was the identity here, and CPython reuses freed addresses: a new loop
    allocated where a closed one lived compared equal, the teardown was skipped, and the
    subsystem went on using a client bound to a loop that no longer runs.

    A weak reference cannot make that mistake — a dead loop dereferences to None.
    """
    conn = RedisConnection("test")

    class _Dead:
        pass

    dead = _Dead()
    conn._loop = __import__("weakref").ref(dead)
    conn._client = object()
    del dead  # the loop goes away, its address free for reuse

    assert conn._loop() is None, "a collected loop should dereference to None"
