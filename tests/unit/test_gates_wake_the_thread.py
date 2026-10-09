"""Every write a durable stream waits on wakes the thread it watches.

A stream tailing a durable run, or reattached to a thread with one in flight, watches three things
the session log does not carry: the run's status, the approvals it is blocked on, and the client
tool requests it is waiting for. They published nothing, so those streams were pinned to the short
poll ceiling and found each one by polling. Now each writer announces, and the streams relax to the
long ceiling like any other -- which is only safe while every writer here keeps announcing. A
writer that stops costs a person up to a minute before they are asked; these fail first.

Through a real `thread_watch`, not a patched `notify_appended`: a wake has to reach the channel the
stream subscribes to -- `durable_thread(...)` for a run -- and a patch would miss a writer that
announced through another reference, and pass a "wakes nobody" check by never looking.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from felix.config import Settings

SETTINGS = Settings(database_url="memory://gates-wake", object_store="memory")


async def wakes(tenant_id: str, thread: str, write: Callable[[], Awaitable[Any]]) -> bool:
    """Whether `write` wakes a stream watching `thread`."""
    from felix.session.notify import thread_watch

    async with thread_watch(tenant_id, thread) as watch:
        await write()
        return (await watch.wait(timeout=0.05)).woken


def _stream_thread(tenant_id: str, fiber: dict[str, Any]) -> str:
    """The thread `durable_run_gen` watches for this run -- the contract, not the fiber's helper."""
    from felix_api.routes._streaming import durable_thread

    return durable_thread(tenant_id, {"thread_id": fiber.get("thread_id"), "fiber_id": fiber["id"]})


async def test_an_approval_opening_wakes_its_thread() -> None:
    from felix.approvals.store import create_pending

    async def write() -> None:
        await create_pending(
            SETTINGS, "acme", tool_name="w", call_signature="s1", thread_id="acme:t1", tool_call_id="c1"
        )

    assert await wakes("acme", "acme:t1", write)


async def test_an_approval_answered_wakes_its_thread() -> None:
    from felix.approvals.store import create_pending, decide

    row = await create_pending(
        SETTINGS, "acme", tool_name="w", call_signature="s2", thread_id="acme:t2", tool_call_id="c2"
    )
    assert await wakes(
        "acme", "acme:t2", lambda: decide(SETTINGS, "acme", row["id"], decision="approved", decided_by="ops")
    )


async def test_an_approval_wakes_only_its_own_thread() -> None:
    from felix.approvals.store import create_pending

    async def write() -> None:
        await create_pending(SETTINGS, "acme", tool_name="w", call_signature="s3", thread_id="acme:other")

    assert not await wakes("acme", "acme:t3", write)


async def test_a_client_request_opening_and_closing_wakes_its_thread() -> None:
    from felix.tools import client_requests

    assert await wakes(
        "acme",
        "acme:t4",
        lambda: client_requests.record("acme:t4", {"id": "c4"}, timeout=5, tenant_id="acme"),
    )
    assert await wakes("acme", "acme:t4", lambda: client_requests.clear("acme:t4", "c4", tenant_id="acme"))


async def test_the_client_tool_executor_passes_its_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wiring: the one production caller of `record`/`clear` hands over the request's tenant,
    without which `_wake` wakes nobody. Each half on its own: woken by `record` while the tool
    waits on its client, and again by `clear` once it is answered."""
    from felix.context import AuthContext, RequestContext, async_run_with_context
    from felix.session.notify import thread_watch
    from felix.tools import client_bridge
    from felix.tools.types import ToolInvocationCtx

    async with thread_watch("acme", "acme:t5") as watch:
        woken_while_waiting: list[bool] = []

        async def answered(*a: Any, **kw: Any) -> client_bridge.ClientToolResult:
            woken_while_waiting.append((await watch.wait(timeout=0.05)).woken)
            return client_bridge.ClientToolResult(content="picked")

        async def no_side_event(*a: Any, **kw: Any) -> None:
            return None

        monkeypatch.setattr(client_bridge, "wait_for_result", answered)
        monkeypatch.setattr(client_bridge, "emit_side_event", no_side_event)
        executor = client_bridge._ClientToolExecutor(name="pick", timeout_seconds=5)
        ctx = RequestContext(settings=SETTINGS, auth=AuthContext(tenant_id="acme", scopes=frozenset()))
        async with async_run_with_context(ctx):
            assert (
                await executor.execute({}, ToolInvocationCtx(thread_id="acme:t5", tool_call_id="c5"))
                == "picked"
            )
        woken_after = (await watch.wait(timeout=0.05)).woken

    assert woken_while_waiting == [True], "recording the request woke nobody"
    assert woken_after, "clearing the request woke nobody"


@pytest.mark.parametrize("named", [True, False], ids=["thread", "anonymous"])
async def test_a_fiber_status_save_wakes_the_stream_tailing_its_run(named: bool) -> None:
    from felix.durability import fibers

    row = await fibers.create_fiber(
        SETTINGS, "acme", kind="durable_chat", thread_id="acme:t6" if named else None
    )
    row["status"] = "completed"
    assert await wakes("acme", _stream_thread("acme", row), lambda: fibers._save_fiber(SETTINGS, row))


async def test_a_stale_fiber_write_wakes_nobody() -> None:
    """A save that lost the version race wrote nothing, so there is nothing to announce."""
    from felix.durability import fibers

    row = await fibers.create_fiber(SETTINGS, "acme", kind="durable_chat", thread_id="acme:t7")
    stale = dict(row)
    row["status"] = "running"
    await fibers._save_fiber(SETTINGS, row)
    stale["status"] = "completed"
    assert not await wakes("acme", "acme:t7", lambda: fibers._save_fiber(SETTINGS, stale))


def test_a_durable_wait_never_runs_past_the_runs_deadline() -> None:
    """Expiry is a time, not a write: nothing announces it. With the long ceiling, the wait is what
    bounds how late the stream reports it."""
    from felix_api.routes._streaming import wait_before_deadline

    now = 1_000.0
    assert wait_before_deadline(60.0, None, floor=1.0, now_s=now) == 60.0
    assert wait_before_deadline(60.0, (now + 5) * 1000, floor=1.0, now_s=now) == 5.0
    # Never longer than the pacing asked for, however far off the deadline is.
    assert wait_before_deadline(5.0, (now + 60) * 1000, floor=1.0, now_s=now) == 5.0
    # Close to it, never under the floor.
    assert wait_before_deadline(60.0, (now + 0.2) * 1000, floor=1.0, now_s=now) == 1.0
    # Past it, the pacing's own wait: a run still held past its deadline is not polled once a
    # second for as long as it is held.
    assert wait_before_deadline(60.0, (now - 5) * 1000, floor=1.0, now_s=now) == 60.0
