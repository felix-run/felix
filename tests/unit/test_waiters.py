"""Tests for the waiter subsystem's fallback path."""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from felix.waiters import MAX_LOCAL_SIGNAL_FIRST, _local, signal, wait


@pytest.fixture(autouse=True)
async def _clear_local() -> None:
    """Clear the in-process waiter dict before each test."""
    _local.clear()
    yield
    _local.clear()


async def test_signal_first_entries_are_bounded() -> None:
    """Signal-first entries in the fallback dict are capped at MAX_LOCAL_SIGNAL_FIRST.

    When Redis is unavailable and signals arrive before their corresponding waits, the
    fallback stores completed futures in `_local`. Without a bound, an authenticated
    caller POSTing `/chat/tool_result` with random `tool_call_id`s grows the dict
    without limit.

    This test verifies that after signaling MAX_LOCAL_SIGNAL_FIRST + 100 times with
    distinct names, the dict contains at most MAX_LOCAL_SIGNAL_FIRST signal-first
    entries (completed futures).
    """
    # Signal more than the cap with distinct names
    num_signals = MAX_LOCAL_SIGNAL_FIRST + 100
    for i in range(num_signals):
        await signal(f"test_signal_{i}", {"index": i})

    # Count signal-first entries (completed futures)
    done_count = sum(1 for f in _local.values() if f.done())

    # The dict should not exceed the cap
    assert done_count <= MAX_LOCAL_SIGNAL_FIRST, (
        f"Expected at most {MAX_LOCAL_SIGNAL_FIRST} signal-first entries, but found {done_count}"
    )

    # Verify the cap was actually enforced (we should have evicted some)
    assert done_count == MAX_LOCAL_SIGNAL_FIRST, (
        f"Expected exactly {MAX_LOCAL_SIGNAL_FIRST} signal-first entries after "
        f"signaling {num_signals} times, but found {done_count}"
    )


async def test_active_waits_are_not_counted_toward_cap() -> None:
    """Active waits (futures created by wait() that aren't resolved) don't count toward the cap.

    The cap applies only to signal-first entries. Active waits are bounded by the number
    of concurrent requests the server can handle, which is already capped by Granian.
    """
    # Create some active waits (futures that are not done)
    wait_tasks = []
    for i in range(10):
        wait_tasks.append(asyncio.create_task(wait(f"active_wait_{i}", timeout=10)))

    # Give the waits time to register
    await asyncio.sleep(0.01)

    # Now signal MAX_LOCAL_SIGNAL_FIRST times with different names
    for i in range(MAX_LOCAL_SIGNAL_FIRST):
        await signal(f"signal_first_{i}", {"index": i})

    # The active waits should still be in _local
    active_count = sum(1 for f in _local.values() if not f.done())
    assert active_count == 10, f"Expected 10 active waits, found {active_count}"

    # And we should have MAX_LOCAL_SIGNAL_FIRST signal-first entries
    done_count = sum(1 for f in _local.values() if f.done())
    assert done_count == MAX_LOCAL_SIGNAL_FIRST

    # Clean up the wait tasks
    for task in wait_tasks:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_signal_first_then_wait_still_works() -> None:
    """A signal-first entry that survives eviction can still be retrieved by wait().

    This verifies that the eviction mechanism doesn't break the normal signal-then-wait
    flow when the signal arrives first and the wait arrives before eviction.
    """
    # Signal first
    await signal("test_key", {"message": "hello"})

    # Wait should retrieve it
    result = await wait("test_key", timeout=1.0)
    assert result == {"message": "hello"}

    # The entry should be cleaned up after wait retrieves it
    assert "test_key" not in _local


async def test_evicted_signal_behaves_like_no_signal() -> None:
    """A signal-first entry that is evicted before its wait arrives behaves like no signal.

    This is the at-most-once contract the fallback already provides: if a signal is lost
    (evicted, or never stored because Redis was down), the wait times out.
    """
    # Fill the dict to the cap
    for i in range(MAX_LOCAL_SIGNAL_FIRST):
        await signal(f"filler_{i}", {"index": i})

    # Signal one more time - this should evict the oldest (filler_0)
    await signal("new_signal", {"message": "new"})

    # Waiting for the evicted signal should time out
    result = await wait("filler_0", timeout=0.1)
    assert result is None, "Expected timeout for evicted signal"

    # Waiting for the new signal should succeed
    result = await wait("new_signal", timeout=0.1)
    assert result == {"message": "new"}


async def test_wait_then_signal_is_unaffected() -> None:
    """The normal wait-then-signal flow is unaffected by the cap.

    When wait() creates a future and signal() resolves it, that future is never counted
    toward the cap because it's not a signal-first entry.
    """
    # Start a wait
    wait_task = asyncio.create_task(wait("normal_flow", timeout=2.0))

    # Give it time to register
    await asyncio.sleep(0.01)

    # Signal should resolve it
    await signal("normal_flow", {"status": "resolved"})

    # Wait should complete successfully
    result = await wait_task
    assert result == {"status": "resolved"}

    # The entry should be cleaned up
    assert "normal_flow" not in _local
