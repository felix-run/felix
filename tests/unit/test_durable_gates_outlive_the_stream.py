"""What a durable run is blocked on reaches its client after the run's own stream has closed.

felix-run/felix#530. A durable `POST /chat/stream` closed at the run's `expires_at` (300s by
default) whether or not the run had stopped, and expiry is checked only between steps -- a
durable chat is one step -- so the run went on. `cowork`'s approvals wait 600s. Everything
the run asked for after the close reached nobody: only that stream announced a pending
`tool_request` or `approval_required`, and `GET /chat/stream/{thread_id}` tailed the session
log alone. On a production thread three write approvals timed out that way, and the writes
behind them could not have run if they had been approved.

Three changes, pinned here:

- the reattach stream announces the gates the thread's run is blocked on;
- it stays open past its idle limit while a durable run is in flight;
- the durable stream closes at the deadline only if no worker still holds the run.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from felix.config import Settings
from felix.durability import fibers
from felix.tools import client_requests

from tests.support.durable_stream import (
    durable_settings,
    force_durable,
    pending_gate,
    post_stream,
    stub_fiber,
)
from tests.support.factories import app_client
from tests.support.sse import sse_blocks, sse_event_names


async def _reattach(client: Any, thread: str) -> str:
    body = ""
    async with client.stream("GET", f"/chat/stream/{thread}") as resp:
        assert resp.status_code == 200, (resp.status_code, await resp.aread())
        async for chunk in resp.aiter_text():
            body += chunk
    return body


async def _durable_run_on(settings: Settings, thread: str, *, expires_in_ms: int = 60_000) -> dict[str, Any]:
    """A durable run in flight on `thread`, as the enqueue leaves one: pending, with no worker."""
    return await fibers.create_fiber(
        settings,
        "default",
        kind="durable_chat",
        state={"steps": [{"op": "complete"}], "cursor": 0, "expires_at": fibers.now_ms() + expires_in_ms},
        thread_id=thread,
        exclusive_on_thread=True,
    )


def _finish(fiber_id: str) -> None:
    fibers._memory_fibers[("default", fiber_id)]["status"] = "completed"


# --- the reattach stream asks what the run is waiting on -----------------------------------


@pytest.mark.asyncio
async def test_a_reattach_is_asked_to_run_the_client_tool_the_run_is_waiting_on() -> None:
    settings = durable_settings()
    thread = "default:reattach-tool"
    request = {"id": "call_w", "name": "local_write", "args": {"path": "a.md"}, "thread_id": thread}
    await client_requests.record(thread, request, timeout=300)
    try:
        async with app_client(settings) as client:
            body = await _reattach(client, "reattach-tool")
    finally:
        await client_requests.clear(thread, "call_w")

    asked = [p["data"] for _, p in sse_blocks(body) if p.get("event") == "tool_request"]
    # Once, however many polls saw it pending: the stream's own dedupe.
    assert asked == [request], f"expected one tool_request, got {asked} in {sse_event_names(body)}"


@pytest.mark.asyncio
async def test_a_reattach_is_asked_to_decide_the_approval_the_run_is_waiting_on() -> None:
    settings = durable_settings()
    thread = "default:reattach-gate"
    await pending_gate(settings, thread, tool_name="local_write", tool_call_id="call_g", ttl_seconds=600)

    async with app_client(settings) as client:
        body = await _reattach(client, "reattach-gate")

    gates = [p["data"] for _, p in sse_blocks(body) if p.get("event") == "approval_required"]
    assert [g["tool_call_id"] for g in gates] == ["call_g"], sse_event_names(body)
    assert gates[0]["thread_id"] == thread


@pytest.mark.asyncio
async def test_a_reattach_is_not_told_another_threads_gates() -> None:
    settings = durable_settings()
    await pending_gate(settings, "default:theirs", call_signature="sig-theirs", ttl_seconds=600)
    await client_requests.record("default:theirs", {"id": "call_t", "name": "local_write"}, timeout=300)
    try:
        async with app_client(settings) as client:
            body = await _reattach(client, "mine")
    finally:
        await client_requests.clear("default:theirs", "call_t")

    names = sse_event_names(body)
    assert "approval_required" not in names and "tool_request" not in names, names


@pytest.mark.asyncio
async def test_a_reattach_holds_open_while_a_durable_run_is_in_flight() -> None:
    """The idle limit (0.2s here) is for an idle thread. A durable run blocked on a person is
    idle by that measure for as long as nobody answers, and closing then is closing exactly
    when the client needs the stream."""
    settings = durable_settings()
    run = await _durable_run_on(settings, "default:held")

    async def finish_later() -> None:
        await asyncio.sleep(0.8)
        _finish(str(run["id"]))

    async with app_client(settings) as client:
        started = time.monotonic()
        finisher = asyncio.create_task(finish_later())
        await _reattach(client, "held")
        lasted = time.monotonic() - started
        await finisher

    assert lasted >= 0.8, f"the reattach closed after {lasted:.2f}s with the run still in flight"


@pytest.mark.asyncio
async def test_a_reattach_with_no_run_in_flight_still_closes_when_idle() -> None:
    settings = durable_settings()
    async with app_client(settings) as client:
        started = time.monotonic()
        await _reattach(client, "idle")
    assert time.monotonic() - started < 5.0


# --- the durable stream's deadline ----------------------------------------------------------


@pytest.mark.asyncio
async def test_the_durable_stream_outlives_its_deadline_while_a_worker_holds_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Past `expires_at` mid-step, the run is still running: report its end, not the deadline."""
    settings = durable_settings()
    force_durable(monkeypatch)
    # The stub run reports `running` three times, then `completed`; its deadline is long past.
    stub_fiber(monkeypatch, statuses=["running", "running", "running"], expires_at=1)
    # And a worker holds the fiber the stream will look up, past its expiry.
    fibers._memory_fibers[("default", "fiber-1")] = {
        "tenant_id": "default",
        "id": "fiber-1",
        "status": "running",
        "state_json": {"expires_at": 1},
        "lease_until": fibers.now_ms() + 60_000,
    }

    async with app_client(settings) as client:
        body = await post_stream(client, "past-deadline")

    names = sse_event_names(body)
    assert "final" in names, f"the stream gave up on a run a worker still held: {names}"
    assert "run_expired:fiber-1" not in body


@pytest.mark.asyncio
async def test_the_durable_stream_still_says_expired_when_nothing_holds_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = durable_settings()
    force_durable(monkeypatch)
    stub_fiber(monkeypatch, statuses=["running", "running"], expires_at=1)
    fibers._memory_fibers[("default", "fiber-1")] = {
        "tenant_id": "default",
        "id": "fiber-1",
        "status": "running",
        "state_json": {"expires_at": 1},
        "lease_until": None,
    }

    async with app_client(settings) as client:
        body = await post_stream(client, "unheld")

    assert "run_expired:fiber-1" in body
