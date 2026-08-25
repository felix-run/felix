"""A reattached-but-quiet stream stops asking every second.

`chat_stream_resume` polled `get_events` at a fixed 1 Hz per client until 300 seconds
of silence, each iteration checking out a pooled connection to run an indexed range
scan and learn nothing. One hundred reattached tabs is a sustained 100 queries/second
of pure polling, against a pool whose ceiling only became configurable in #66.

Backing off is not free: it costs first-event latency, because a thread that goes quiet
and then produces makes the client wait up to the current delay. The moment a user is
most likely to act is right after they reattach — so the ramp holds at the floor for
the first thirty seconds and only decays past that. The load this finding is about
comes from tabs left open for minutes, not from the first few seconds of one.

Modelled over one 300-second idle window, floor 1s and ceiling 10s:

    polls: 61, against 300 at a fixed 1 Hz — 4.9x fewer queries per idle client
    first-event latency during the first 30s: 1.0s, unchanged
"""

from __future__ import annotations

import pytest
from felix.config import Settings

# Imported defensively so this module still collects against a version without the
# backoff. The arithmetic tests then skip -- there is no arithmetic to test -- but the
# wiring test below still runs and still fails, which is the assertion that matters.
try:
    from felix_api.routes.chat import (
        POLL_BACKOFF_FACTOR,
        POLL_BACKOFF_GRACE_SECONDS,
        _next_poll_delay,
    )

    HAS_BACKOFF = True
except ImportError:  # pragma: no cover - only on a version predating the backoff
    POLL_BACKOFF_FACTOR = POLL_BACKOFF_GRACE_SECONDS = 0.0
    _next_poll_delay = None  # type: ignore[assignment]
    HAS_BACKOFF = False

pytestmark_arithmetic = pytest.mark.skipif(not HAS_BACKOFF, reason="no backoff to test")

FLOOR, CEILING = 1.0, 10.0


def _ramp(floor: float = FLOOR, ceiling: float = CEILING) -> list[tuple[float, float]]:
    """(idle_seconds, delay_used) for one uninterrupted quiet window."""
    idle, delay, out = 0.0, floor, []
    while idle < 300.0:
        out.append((idle, delay))
        idle += delay
        delay = _next_poll_delay(idle, delay, floor=floor, ceiling=ceiling)
    return out


@pytestmark_arithmetic
def test_the_first_thirty_seconds_are_not_slowed_at_all() -> None:
    """The whole cost of this change lands on first-event latency, so the window where
    a user is most likely to act must be untouched."""
    for idle, delay in _ramp():
        if idle < POLL_BACKOFF_GRACE_SECONDS:
            assert delay == FLOOR, f"backed off to {delay}s only {idle}s into the stream"


@pytestmark_arithmetic
def test_it_decays_after_the_grace_window() -> None:
    late = [delay for idle, delay in _ramp() if idle > POLL_BACKOFF_GRACE_SECONDS * 2]
    assert late, "the ramp never got past the grace window"
    assert min(late) > FLOOR, "the poll never decayed at all"


@pytestmark_arithmetic
def test_the_delay_never_passes_the_ceiling_or_drops_below_the_floor() -> None:
    delays = [delay for _idle, delay in _ramp()]
    assert max(delays) <= CEILING
    assert min(delays) >= FLOOR


@pytestmark_arithmetic
def test_the_idle_window_costs_far_fewer_queries() -> None:
    """The point of the finding, stated as the number it is about."""
    polls = len(_ramp())
    fixed_rate = int(300.0 / FLOOR)
    assert polls < fixed_rate / 3, f"{polls} polls per idle window against {fixed_rate} before"


@pytestmark_arithmetic
def test_activity_returns_the_stream_to_the_floor() -> None:
    """`_next_poll_delay` is only consulted on an empty round; the loop resets to the
    floor when events arrive. Asserted here so the reset cannot quietly become a decay
    from wherever the delay had climbed to."""
    assert _next_poll_delay(0.0, CEILING, floor=FLOOR, ceiling=CEILING) == FLOOR


@pytestmark_arithmetic
def test_a_ceiling_below_the_floor_cannot_speed_the_poll_up() -> None:
    """Misconfiguration must not turn a backoff into a tighter loop than the floor."""
    assert _next_poll_delay(999.0, FLOOR, floor=FLOOR, ceiling=0.1) <= FLOOR


@pytestmark_arithmetic
def test_the_ramp_is_gentle_enough_to_be_worth_the_grace_window() -> None:
    """A factor so steep that the first post-grace step lands on the ceiling would make
    the grace window the only thing protecting latency."""
    assert 1.0 < POLL_BACKOFF_FACTOR <= 2.0
    first_step = _next_poll_delay(POLL_BACKOFF_GRACE_SECONDS + 1, FLOOR, floor=FLOOR, ceiling=CEILING)
    assert first_step < CEILING, "the first decay step jumps straight to the ceiling"


@pytestmark_arithmetic
def test_the_ceiling_is_configurable_and_bounded() -> None:
    s = Settings(database_url="memory://x", stream_resume_poll_max_seconds=30.0)
    assert s.stream_resume_poll_max_seconds == 30.0
    with pytest.raises(ValueError):
        Settings(database_url="memory://x", stream_resume_poll_max_seconds=0.0)


@pytestmark_arithmetic
def test_idle_accounting_follows_the_actual_wait() -> None:
    """The disconnect is 300 seconds of real silence.

    The loop used to add `poll` per iteration, which was the wait. With a varying delay
    that identity breaks, and adding the floor instead would stretch a 300-second limit
    to nearly 50 minutes of wall clock before a connection is released.
    """
    ramp = _ramp()
    total = sum(delay for _idle, delay in ramp)
    assert 300.0 <= total < 310.0, f"the ramp accumulated {total}s of idle, not ~300s"
    naive = len(ramp) * FLOOR
    assert naive < 100.0, "sanity: counting the floor per iteration would badly undercount"


# --- the wiring, not just the arithmetic --------------------------------------------


@pytest.mark.asyncio
async def test_the_loop_actually_waits_the_backed_off_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The arithmetic above is only worth anything if the loop uses it.

    Passing `poll` to `sleep` while feeding `delay` to the accounting would keep the
    1 Hz cadence and still look correct in every unit test of `_next_poll_delay`. So
    this records what the loop actually sleeps for.
    """
    from felix.session.notify import Wake
    from felix_api.app import create_app
    from felix_api.routes import chat as chat_mod
    from httpx import ASGITransport, AsyncClient

    slept: list[float] = []

    async def _record(_tenant: str, _thread: str, *, timeout: float):
        """Stand in for the wait, recording what the loop asked to wait *for*.

        The loop used to `asyncio.sleep(delay)`; it now waits on a thread notification
        with the same delay as its timeout. Instrumenting the wait rather than the
        sleep is the difference between measuring the backoff and measuring nothing --
        this test hung for 120 seconds when the mechanism changed underneath it.
        """
        slept.append(timeout)
        return Wake(woken=False, by_notification=False)

    monkeypatch.setattr(chat_mod, "wait_for_events", _record)

    settings = Settings(
        allow_insecure=True,
        auth_mode="none",
        environment="development",
        object_store="memory",
        database_url="memory://backoff",
        redis_url="",
        stream_resume_idle_seconds=120.0,
        stream_resume_poll_seconds=1.0,
        # Only on a version that has it; the route defaults to 10.0 either way.
        **({"stream_resume_poll_max_seconds": 10.0} if HAS_BACKOFF else {}),
    )
    app = create_app(settings=settings, plugins=[])
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test", timeout=30.0)
    async with client.stream("GET", "/chat/stream/resume?thread_id=quiet") as resp:
        assert resp.status_code == 200
        async for _chunk in resp.aiter_text():
            pass

    assert slept, "the resume loop never slept"
    assert slept[0] == 1.0, f"the first wait should be the floor, was {slept[0]}"
    assert max(slept) > 1.0, "the loop never backed off; it is probably sleeping `poll`"
    assert max(slept) <= 10.0, f"slept past the configured ceiling: {max(slept)}"
    # 120s of idle at a fixed 1 Hz would be 120 waits.
    assert len(slept) < 60, f"{len(slept)} polls to cover 120s of silence"
