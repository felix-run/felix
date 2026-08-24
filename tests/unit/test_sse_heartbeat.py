"""`with_heartbeat` semantics.

The implementation runs the upstream stream in a pump task feeding a bounded queue,
which is a real change in shape from awaiting each event under a timeout. These lock
down the four behaviours that shape has to preserve: events arrive in order, an
upstream failure reaches the consumer, a consumer that walks away tears the run down,
and a quiet stream still gets heartbeats.
"""

from __future__ import annotations

import asyncio

import pytest
from felix_api.routes._sse import HEARTBEAT, HEARTBEAT_QUEUE_MAXSIZE, with_heartbeat


async def _drain(stream, **kwargs):
    return [item async for item in with_heartbeat(stream, **kwargs)]


async def _counter(n: int):
    for i in range(n):
        yield i


@pytest.mark.asyncio
async def test_events_pass_through_in_order() -> None:
    assert await _drain(_counter(50)) == list(range(50))


@pytest.mark.asyncio
async def test_empty_stream_terminates() -> None:
    assert await _drain(_counter(0)) == []


@pytest.mark.asyncio
async def test_stream_ending_on_a_full_queue_still_terminates() -> None:
    """The sentinel must not be dropped when the last event filled the queue.

    Bounding the queue introduced a way for the stream to hang: if the final event
    left the queue full, a non-blocking put of the sentinel is discarded, the consumer
    drains everything, finds nothing more, and emits heartbeats forever instead of
    returning. Exercised at exactly maxsize, and one either side of it.
    """
    for n in (HEARTBEAT_QUEUE_MAXSIZE - 1, HEARTBEAT_QUEUE_MAXSIZE, HEARTBEAT_QUEUE_MAXSIZE + 1):
        got = await asyncio.wait_for(_drain(_counter(n)), timeout=5.0)
        assert got == list(range(n)), f"stream of {n} events did not terminate cleanly"


@pytest.mark.asyncio
async def test_upstream_exception_reaches_the_consumer() -> None:
    """chat.py turns this into an `event: error` frame; swallowing it ends the
    response under a 200 with no way to tell success from failure."""

    async def boom():
        yield "a"
        raise RuntimeError("upstream exploded")

    seen: list[object] = []
    with pytest.raises(RuntimeError, match="upstream exploded"):
        async for item in with_heartbeat(boom()):
            seen.append(item)
    assert seen == ["a"], "events before the failure should still be delivered"


@pytest.mark.asyncio
async def test_consumer_break_cancels_the_upstream_run() -> None:
    """A hung-up client must not leave the agent loop burning tokens."""
    cancelled = asyncio.Event()

    async def long_run():
        try:
            for i in range(10_000):
                await asyncio.sleep(0)
                yield i
        except asyncio.CancelledError:
            cancelled.set()
            raise

    agen = with_heartbeat(long_run())
    async for item in agen:
        if item == 3:
            break
    await agen.aclose()

    await asyncio.wait_for(cancelled.wait(), timeout=5.0)


@pytest.mark.asyncio
async def test_quiet_stream_still_emits_heartbeats() -> None:
    """The whole point of the wrapper: a long tool call emits nothing, and a proxy
    idle timeout would close a perfectly healthy run."""

    async def quiet():
        await asyncio.sleep(0.25)
        yield "late"

    got = await asyncio.wait_for(_drain(quiet(), interval=0.05), timeout=5.0)
    assert got[-1] == "late"
    assert got.count(HEARTBEAT) >= 2, f"expected heartbeats during the silence, got {got}"


@pytest.mark.asyncio
async def test_busy_stream_emits_no_heartbeats() -> None:
    """A stream delivering events faster than the interval should never look idle."""
    got = await _drain(_counter(200), interval=5.0)
    assert HEARTBEAT not in got


@pytest.mark.asyncio
async def test_the_queue_bound_actually_applies_backpressure() -> None:
    """The bound is the design claim, so it needs pinning on its own.

    Setting HEARTBEAT_QUEUE_MAXSIZE to 0 — an unbounded queue, no backpressure at
    all — passes every other test in this file, because the full-queue test is
    parameterised by the very constant it is meant to exercise. An unbounded queue
    turns a slow SSE consumer into unbounded memory growth in the API process, so
    "the pump blocks and the agent loop feels it" has to be asserted directly.
    """
    assert HEARTBEAT_QUEUE_MAXSIZE >= 8, "a tiny bound would make the other tests vacuous"

    produced = 0

    async def counted():
        nonlocal produced
        for i in range(HEARTBEAT_QUEUE_MAXSIZE * 4):
            produced += 1
            yield i

    agen = with_heartbeat(counted())
    await agen.__anext__()  # start the pump, then stop draining
    for _ in range(50):
        await asyncio.sleep(0)

    # One consumed, `maxsize` parked in the queue, and the pump blocked on the next
    # put. Anything materially above that means the bound is not holding.
    assert produced <= HEARTBEAT_QUEUE_MAXSIZE + 2, (
        f"pump produced {produced} events with a stalled consumer and a "
        f"{HEARTBEAT_QUEUE_MAXSIZE}-slot queue — backpressure is not applying"
    )
    await agen.aclose()
