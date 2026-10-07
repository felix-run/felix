"""A client tool's request is recorded where the stream serving a durable run can read it.

The side event a client tool emits reaches only a stream in the agent's own process, and a
durable run's agent is in the worker. These pin the record that stands in for it: present for
exactly as long as the run waits, scoped to its thread, and gone however the wait ends.
"""

from __future__ import annotations

import asyncio

import pytest
from felix.manifests.schema import ClientToolRef
from felix.tools import client_requests
from felix.tools.client_bridge import complete_result, tools_from_client_refs
from felix.tools.types import ToolInvocationCtx


def _tool(timeout: float | None = None):
    (tool,) = tools_from_client_refs(
        [ClientToolRef(name="local_read", description="Read a file", timeout_seconds=timeout)]
    )
    return tool


@pytest.mark.asyncio
async def test_a_waiting_client_tool_is_pending_until_it_is_answered() -> None:
    thread = "default:pending-answered"
    task = asyncio.create_task(
        _tool().executor.execute(
            {"path": "README.md"}, ToolInvocationCtx(thread_id=thread, tool_call_id="call_r")
        )
    )
    await asyncio.sleep(0.05)

    (request,) = await client_requests.pending(thread)
    assert request["id"] == "call_r"
    assert request["name"] == "local_read"
    assert request["args"] == {"path": "README.md"}
    assert request["thread_id"] == thread
    assert request["transport"] == "client"

    await complete_result(thread, "call_r", "# readme")
    assert await task == "# readme"
    assert await client_requests.pending(thread) == [], "an answered request was still pending"


@pytest.mark.asyncio
async def test_a_request_that_timed_out_is_no_longer_pending() -> None:
    """The run has already been told `[error/timeout]`; asking the client now helps nobody."""
    thread = "default:pending-timeout"
    out = await _tool(timeout=0.1).executor.execute(
        {"path": "x"}, ToolInvocationCtx(thread_id=thread, tool_call_id="call_t")
    )
    assert "timeout" in str(out)
    assert await client_requests.pending(thread) == []


@pytest.mark.asyncio
async def test_a_cancelled_wait_does_not_leave_its_request_behind() -> None:
    """A run torn down mid-wait must not leave a prompt for the next stream to announce."""
    thread = "default:pending-cancel"
    task = asyncio.create_task(
        _tool().executor.execute({"path": "x"}, ToolInvocationCtx(thread_id=thread, tool_call_id="call_c"))
    )
    await asyncio.sleep(0.05)
    assert await client_requests.pending(thread)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await client_requests.pending(thread) == []


@pytest.mark.asyncio
async def test_pending_is_scoped_to_the_thread_including_threads_with_colons() -> None:
    """`{tenant}:fiber:{id}` carries an extra colon; the key must not let it alias another."""
    await client_requests.record("acme:fiber:F1", {"id": "call_9", "name": "n"}, timeout=60)
    try:
        assert await client_requests.pending("acme:fiber") == []
        assert await client_requests.pending("acme:other") == []
        assert [r["id"] for r in await client_requests.pending("acme:fiber:F1")] == ["call_9"]
    finally:
        await client_requests.clear("acme:fiber:F1", "call_9")


@pytest.mark.asyncio
async def test_a_lapsed_request_is_not_pending() -> None:
    thread = "default:pending-lapsed"
    await client_requests.record(thread, {"id": "call_l", "name": "n"}, timeout=-1)
    try:
        assert await client_requests.pending(thread) == []
    finally:
        await client_requests.clear(thread, "call_l")
