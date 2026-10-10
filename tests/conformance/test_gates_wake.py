"""Each store's gate writes wake the thread on every backend.

`tests/unit/test_gates_wake_the_thread.py` pins the writers on `memory://`; the Postgres arms of
`approvals.decide`, `fibers._save_fiber`, the claim and `_record_attempt` are separate code paths,
and a stream relaxing to the long poll ceiling is only safe if they announce too. Checked through a
real `thread_watch`, as the unit file is. Add a backend to `BACKENDS` and it inherits.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest

BACKENDS = ["memory", "postgres"]
parametrized = pytest.mark.parametrize("store_settings", BACKENDS, indirect=True)


async def wakes(tenant_id: str, thread: str, write: Callable[[], Awaitable[Any]]) -> bool:
    from felix.session.notify import thread_watch

    async with thread_watch(tenant_id, thread) as watch:
        await write()
        return (await watch.wait(timeout=0.05)).woken


@parametrized
async def test_an_approval_opened_and_answered_wakes_its_thread(store_settings: Any) -> None:
    from felix.approvals.store import create_pending, decide

    row: dict[str, Any] = {}

    async def open_it() -> None:
        row.update(
            await create_pending(
                store_settings,
                "acme",
                tool_name="w",
                call_signature="s",
                thread_id="acme:g1",
                tool_call_id="c",
            )
        )

    assert await wakes("acme", "acme:g1", open_it)
    assert await wakes(
        "acme",
        "acme:g1",
        lambda: decide(store_settings, "acme", row["id"], decision="denied", decided_by="ops"),
    )


@parametrized
async def test_a_fiber_status_save_wakes_its_thread(store_settings: Any) -> None:
    from felix.durability import fibers

    row = await fibers.create_fiber(store_settings, "acme", kind="durable_chat", thread_id="acme:g2")
    row["status"] = "completed"
    assert await wakes("acme", "acme:g2", lambda: fibers._save_fiber(store_settings, row))


@parametrized
async def test_a_stale_fiber_write_wakes_nobody(store_settings: Any) -> None:
    """A save that lost the version race wrote nothing, so there is nothing to announce."""
    from felix.durability import fibers

    row = await fibers.create_fiber(store_settings, "acme", kind="durable_chat", thread_id="acme:g3")
    stale = dict(row)
    row["status"] = "running"
    await fibers._save_fiber(store_settings, row)
    stale["status"] = "completed"
    assert not await wakes("acme", "acme:g3", lambda: fibers._save_fiber(store_settings, stale))


@parametrized
async def test_a_claim_and_the_fallback_status_write_wake_the_thread(store_settings: Any) -> None:
    """The two other status writes: the claim that sets `running`, and `_record_attempt`, which
    lands `dead` when the versioned save itself failed."""
    from felix.durability import fibers

    await fibers.create_fiber(store_settings, "acme", kind="durable_chat", thread_id="acme:g4")
    claimed: list[dict[str, Any]] = []

    async def claim() -> None:
        claimed.extend(await fibers._claim_due(store_settings))

    assert await wakes("acme", "acme:g4", claim)
    (row,) = [r for r in claimed if r.get("thread_id") == "acme:g4"]
    row["status"] = "dead"
    assert await wakes("acme", "acme:g4", lambda: fibers._record_attempt(store_settings, row))
