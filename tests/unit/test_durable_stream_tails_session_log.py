"""A durable run reports what it is *doing*, not only that it is running.

The transcript of a durable run used to reach a client empty: the answer arrived on the
`final` frame and the tool calls behind it did not, so chat-ui showed a bare reply until
the thread was next hydrated.

The cause is not transport. `felix.side_events` is an in-process queue drained inside the
agent's own loop, so for a durable run both ends are already in the worker — but the
events do not exist to be forwarded anyway: the fiber calls `agent.invoke`, which is
`_run(..., emit_events=False)`, and the deltas and `on_tool_start`/`on_tool_end` pairs are
dropped at the source.

What the fiber *does* produce is the session log. `_append_produced` writes each assistant
turn and its tool results as they land, outside every `emit_events` guard, so a durable run
already persists its progress to shared state as it happens. These tests pin that the
durable stream tails it — the same log, through the same helper, as `GET /chat/stream/{id}`.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from felix.config import Settings
from felix.session.store import get_session_store
from felix.session.types import AppendableEvent
from httpx import ASGITransport, AsyncClient


def _settings(name: str) -> Settings:
    return Settings(
        allow_insecure=True,
        auth_mode="none",
        environment="development",
        object_store="memory",
        database_url=f"memory://{name}",
        redis_url="",
        stream_resume_poll_seconds=0.1,
        stream_resume_poll_max_seconds=0.1,
    )


def _client(settings: Settings) -> AsyncClient:
    from felix_api.app import create_app

    app = create_app(settings=settings, plugins=[])
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", timeout=30.0)


def _blocks(body: str) -> list[tuple[int | None, dict[str, Any]]]:
    """Every data frame as `(id, payload)`, keeping the `id:` the frame carried."""
    out: list[tuple[int | None, dict[str, Any]]] = []
    for block in body.split("\n\n"):
        event_id: int | None = None
        payload: dict[str, Any] | None = None
        for line in block.splitlines():
            if line.startswith("id: "):
                event_id = int(line[4:])
            elif line.startswith("data: ") and line[6:] != "[DONE]":
                payload = json.loads(line[6:])
        if payload is not None:
            out.append((event_id, payload))
    return out


def _names(body: str) -> list[str]:
    return [str(p.get("event")) for _, p in _blocks(body)]


def _session(settings: Settings, thread: str) -> Any:
    return get_session_store(settings, tenant_id="default").open(thread)


async def _append(settings: Settings, thread: str, *events: AppendableEvent) -> None:
    await _session(settings, thread).append_batch(list(events))


def _turn() -> list[AppendableEvent]:
    """One assistant turn with a tool call and its result — the shape a tool card needs."""
    return [
        AppendableEvent(
            kind="message",
            role="assistant",
            content="",
            tool_calls=[{"id": "c1", "type": "function", "function": {"name": "search", "arguments": "{}"}}],
        ),
        AppendableEvent(kind="message", role="tool", content="42", name="search", tool_call_id="c1"),
    ]


def _force_durable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every resolved manifest durable, without editing a bundled file."""
    from felix_api.routes import chat as chat_mod

    real = chat_mod.resolve_tenant_manifest

    async def _resolve(*a: Any, **k: Any) -> Any:
        resolved = await real(*a, **k)
        # A copy: the resolver hands out the object held in its cache, and mutating that
        # one leaves `quick` durable for every later test in the process.
        resolved.manifest = resolved.manifest.model_copy(deep=True)
        resolved.manifest.spec.execution.mode = "durable"
        return resolved

    monkeypatch.setattr(chat_mod, "resolve_tenant_manifest", _resolve)


def _stub_fiber(
    monkeypatch: pytest.MonkeyPatch,
    *,
    on_start: Any = None,
    on_poll: list[Any] | None = None,
    statuses: list[str],
) -> dict[str, Any]:
    """Stand in for the fiber store and the worker.

    `on_start` runs when the run is enqueued and each `on_poll` entry runs before the
    matching status read, which is how a test says "the worker appended this much by
    now" without a worker.
    """
    seen: dict[str, Any] = {"thread_id": None, "polls": 0}
    pending = list(statuses)
    steps = list(on_poll or [])

    async def _start(*_a: Any, **kw: Any) -> dict[str, Any]:
        seen["thread_id"] = kw.get("thread_id")
        if on_start is not None:
            await on_start()
        return {
            "status": "accepted",
            "resume_token": "fiber-1",
            "fiber_id": "fiber-1",
            "expires_at": 1 << 62,
            "thread_id": kw.get("thread_id"),
        }

    async def _get(_settings: Any, _tenant: str, token: str) -> dict[str, Any] | None:
        if token != "fiber-1":
            return None
        if steps:
            step = steps.pop(0)
            if step is not None:
                await step()
        seen["polls"] += 1
        status = pending.pop(0) if pending else "completed"
        return {
            "status": status,
            "fiber_id": token,
            "resume_token": token,
            "expires_at": 1 << 62,
            "final": {"role": "assistant", "content": "42 it is"},
            "error": "",
            "manifest_id": "quick",
        }

    import felix.durability.runs as runs_mod

    monkeypatch.setattr(runs_mod, "start_durable_chat", _start)
    monkeypatch.setattr(runs_mod, "get_durable_run", _get)
    return seen


async def _post_stream(client: AsyncClient, thread: str | None) -> str:
    payload: dict[str, Any] = {"manifest": "quick", "messages": [{"role": "user", "content": "hi"}]}
    if thread is not None:
        payload["thread_id"] = thread
    body = ""
    async with client.stream("POST", "/chat/stream", json=payload) as resp:
        assert resp.status_code == 200, (resp.status_code, await resp.aread())
        async for chunk in resp.aiter_text():
            body += chunk
    return body


@pytest.mark.asyncio
async def test_a_durable_run_streams_its_tool_calls_not_only_its_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The defect itself: the answer arrived and the work behind it did not."""
    settings = _settings("tail-progress")
    thread = "default:tools"
    _force_durable(monkeypatch)

    async def worker_ran_a_turn() -> None:
        await _append(settings, thread, *_turn())

    _stub_fiber(monkeypatch, on_poll=[None, worker_ran_a_turn], statuses=["running", "running"])

    async with _client(settings) as client:
        body = await _post_stream(client, "tools")

    names = _names(body)
    assert "session_event" in names, f"a durable run reported no progress at all: {names}"
    tool = [p for _, p in _blocks(body) if p.get("event") == "session_event" and p["data"]["role"] == "tool"]
    assert tool, "the tool result never reached the client"
    assert tool[0]["data"]["name"] == "search"
    assert tool[0]["data"]["content"] == "42"
    assert names.index("session_event") < names.index("final"), (
        f"progress arrived after the answer it explains: {names}"
    )


@pytest.mark.asyncio
async def test_progress_the_worker_made_before_the_stream_polled_is_not_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cursor has to be taken before the run is enqueued.

    A fiber can be claimed and start appending the moment its row lands. A stream that
    reads the thread's head when it emits `run_accepted` — the obvious place — has
    already skipped whatever the worker wrote in between, and opens at the end of the
    progress it exists to report.
    """
    settings = _settings("tail-race")
    thread = "default:race"
    _force_durable(monkeypatch)
    # History from an earlier turn. The cursor exists so this is *not* replayed.
    await _append(settings, thread, AppendableEvent(kind="message", role="assistant", content="old answer"))

    async def worker_beat_the_stream() -> None:
        await _append(settings, thread, *_turn())

    _stub_fiber(monkeypatch, on_start=worker_beat_the_stream, statuses=["running"])

    async with _client(settings) as client:
        body = await _post_stream(client, "race")

    contents = [p["data"]["content"] for _, p in _blocks(body) if p.get("event") == "session_event"]
    assert "42" in contents, (
        f"events appended between the cursor read and the first poll were lost: {contents}"
    )
    assert "old answer" not in contents, f"the stream replayed history it should have skipped: {contents}"


@pytest.mark.asyncio
async def test_the_cursor_is_monotonic_and_resumable(monkeypatch: pytest.MonkeyPatch) -> None:
    """`id:` is the *next* sequence, the same meaning it has on the reattach stream, so a
    client can hand it straight back as `Last-Event-ID`."""
    settings = _settings("tail-cursor")
    thread = "default:cursor"
    _force_durable(monkeypatch)

    async def worker_ran_a_turn() -> None:
        await _append(settings, thread, *_turn())

    _stub_fiber(monkeypatch, on_poll=[worker_ran_a_turn], statuses=["running"])

    async with _client(settings) as client:
        body = await _post_stream(client, "cursor")

    ids = [i for i, p in _blocks(body) if p.get("event") == "session_event"]
    assert len(ids) >= 2, f"expected a frame per appended event, got {ids}"
    assert all(x is not None for x in ids), "a session_event frame carried no id to resume from"
    assert ids == sorted(ids) and len(set(ids)) == len(ids), f"cursor went backwards or repeated: {ids}"

    seqs = [e.seq for e in await _session(settings, thread).get_events()]
    assert ids[-1] == seqs[-1] + 1, "the last id is not the next sequence the client should ask for"


@pytest.mark.asyncio
async def test_a_run_with_no_thread_tails_the_thread_the_fiber_mints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run started without a `thread_id` is not a run without a thread: the fiber writes
    its transcript to `{tenant}:fiber:{id}` just the same."""
    from felix.durability.fibers import fiber_thread_id

    settings = _settings("tail-anon")
    _force_durable(monkeypatch)
    thread = fiber_thread_id("default", "fiber-1")

    async def worker_ran_a_turn() -> None:
        await _append(settings, thread, *_turn())

    seen = _stub_fiber(monkeypatch, on_poll=[worker_ran_a_turn], statuses=["running"])

    async with _client(settings) as client:
        body = await _post_stream(client, None)

    assert seen["thread_id"] is None, "the request was supposed to carry no thread"
    contents = [p["data"]["content"] for _, p in _blocks(body) if p.get("event") == "session_event"]
    assert "42" in contents, f"an anonymous durable run reported no progress: {contents}"


@pytest.mark.asyncio
async def test_a_turn_appended_just_before_completion_still_precedes_the_final_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fiber writes the transcript and *then* saves `completed`, so the iteration that
    sees the terminal status must drain before it emits `final` — otherwise the last turn
    is lost to the client that was watching it happen."""
    settings = _settings("tail-last")
    thread = "default:last"
    _force_durable(monkeypatch)

    async def worker_finished() -> None:
        await _append(settings, thread, *_turn())

    _stub_fiber(monkeypatch, on_poll=[worker_finished], statuses=["completed"])

    async with _client(settings) as client:
        body = await _post_stream(client, "last")

    names = _names(body)
    assert "session_event" in names, f"the last turn never reached the client: {names}"
    assert names.index("session_event") < names.index("final"), names
    assert body.rstrip().endswith("[DONE]")
