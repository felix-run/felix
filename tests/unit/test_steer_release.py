"""A finished run's in-process queue is released only once nothing is waiting in it.

A steer or follow-up that lands after the run's last drain belongs to the next run; releasing
the queue with it inside is how a message was accepted with 200 and then lost. The HTTP path
cannot stage that race deterministically, so this drives the queue directly — with Redis off,
which is the fallback the in-process queue exists for.
"""

from __future__ import annotations

import pytest
from felix import steer


@pytest.mark.asyncio
async def test_a_message_that_arrived_after_the_last_drain_survives_the_release() -> None:
    await steer.ensure_run_queue("t", "late")
    await steer.enqueue("t", "late", kind="steer", text="prefer metric units")

    await steer.release_run_queue("t", "late")

    assert [m.text for m in await steer.drain_steer("t", "late")] == ["prefer metric units"]
    await steer.release_run_queue("t", "late")
    assert steer._key("t", "late") not in steer._queues, "an empty queue is still released"
