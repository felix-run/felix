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
        # Only the reattach stream reads this, and it is what lets that stream *end*: the
        # durable loop is bounded by the run's terminal status instead. Left at its 300s
        # default the resume test holds the connection for five minutes.
        stream_resume_idle_seconds=0.2,
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


async def _append(settings: Settings, thread: str, *events: AppendableEvent) -> list[str]:
    """Append the way the agent appends, and hand back the event ids it stamped.

    Through `annotate_and_append`, not `append_batch`: the agent loop reaches the store
    that way (`patterns/react.py:_append_produced`), and it is what stamps
    `metadata["event_id"]`. Appending raw leaves the metadata empty, so every frame falls
    to the `seq-N` fallback and the branch production actually takes — a real event id —
    goes untested.
    """
    from felix.session.tree import annotate_and_append

    await annotate_and_append(_session(settings, thread), list(events))
    rows = await _session(settings, thread).get_events()
    return [str((e.metadata or {}).get("event_id") or "") for e in rows[-len(events) :]]


def _turn() -> list[AppendableEvent]:
    """One assistant turn with a tool call and its result, exactly as the agent writes it.

    `{"id", "name", "args"}` and `kind="tool_result"` are `chat_message_to_event`'s own
    output (`session/types.py:149`), not the OpenAI wire shape. Writing the wire shape here
    instead would be a fixture testing a row the product never appends.
    """
    return [
        AppendableEvent(
            kind="message",
            role="assistant",
            content="",
            tool_calls=[{"id": "c1", "name": "search", "args": {"q": "answer"}}],
        ),
        AppendableEvent(kind="tool_result", role="tool", content="42", name="search", tool_call_id="c1"),
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


_UNSET = object()


def _stub_fiber(
    monkeypatch: pytest.MonkeyPatch,
    *,
    on_start: Any = None,
    on_poll: list[Any] | None = None,
    statuses: list[str],
    expires_at: int = 1 << 62,
    error: str = "",
) -> dict[str, Any]:
    """Stand in for the fiber store and the worker.

    `on_start` runs when the run is enqueued and each `on_poll` entry runs before the
    matching status read, which is how a test says "the worker appended this much by
    now" without a worker.

    The *session store* is real (`memory://`) — only the fiber row and the worker are
    scripted, because a test needs to say exactly when the worker appended relative to
    when the stream polled, and a real worker cannot be asked that. The keys returned here
    are pinned against the real `start_durable_chat` by
    `test_the_accepted_shape_matches_what_the_real_start_returns`, so this fixture cannot
    drift into describing a run shape the harness does not produce.
    """
    # A sentinel, not None: `assert seen["thread_id"] is None` is the assertion the
    # anonymous-run test makes, and initialising to None would let it pass vacuously if
    # `_start` were never called at all.
    seen: dict[str, Any] = {"thread_id": _UNSET}
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
            "expires_at": expires_at,
            "thread_id": kw.get("thread_id"),
        }

    async def _get(_settings: Any, _tenant: str, token: str) -> dict[str, Any] | None:
        if token != "fiber-1":
            return None
        if steps:
            step = steps.pop(0)
            if step is not None:
                await step()
        status = pending.pop(0) if pending else "completed"
        return {
            "status": status,
            "fiber_id": token,
            "resume_token": token,
            "expires_at": expires_at,
            "final": {"role": "assistant", "content": "42 it is"},
            "error": error,
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
    # Interleaved, which is the claim. Only the tailed half is new, so it is the half that
    # gets asserted everywhere else — this pins that the status frames it interleaves with
    # did not stop arriving.
    first = names.index("session_event")
    assert "run_status" in names[:first] and "run_status" in names[first:], (
        f"progress did not land between status frames: {names}"
    )
    tool = [p for _, p in _blocks(body) if p.get("event") == "session_event" and p["data"]["role"] == "tool"]
    assert tool, "the tool result never reached the client"
    assert tool[0]["data"]["name"] == "search"
    assert tool[0]["data"]["content"] == "42"
    assert names.index("session_event") < names.index("final"), (
        f"progress arrived after the answer it explains: {names}"
    )


@pytest.mark.asyncio
async def test_a_session_event_carries_what_a_tool_card_is_built_from(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`tool_calls` opens the card and `tool_call_id` closes it. The frame carried neither.

    A client folds these rows with the same function it folds a `snapshot` with: it reads
    `tool_calls` off the assistant message to open a card per call, and matches
    `tool_call_id` on the tool message to attach the result. Without both, an assistant
    turn that called a tool folds to an empty message and the result is dropped — so the
    transcript renders with no tool calls in it at all, which is the thing this whole path
    exists to deliver. The snapshot has carried them all along; only the incremental frame
    was thinner.
    """
    settings = _settings("tail-cards")
    thread = "default:cards"
    _force_durable(monkeypatch)

    stamped: list[str] = []

    async def worker_ran_a_turn() -> None:
        stamped.extend(await _append(settings, thread, *_turn()))

    _stub_fiber(monkeypatch, on_poll=[worker_ran_a_turn], statuses=["running"])

    async with _client(settings) as client:
        body = await _post_stream(client, "cards")

    events = [p["data"] for _, p in _blocks(body) if p.get("event") == "session_event"]
    assistant = next(e for e in events if e["role"] == "assistant")
    tool = next(e for e in events if e["role"] == "tool")

    assert assistant.get("tool_calls"), f"no tool_calls to open a card from: {assistant}"
    call = assistant["tool_calls"][0]
    assert {"id", "name", "args"} <= set(call), f"a card needs id/name/args, got {call}"
    assert call["name"] == "search"
    assert tool.get("tool_call_id") == call["id"], (
        f"the result cannot be matched to the call it answers: {tool}"
    )

    # The *stamped* id, not merely a truthy one. `_session_event_frame` falls back to
    # `seq-N` when an event carries no `event_id`, and a raw append leaves metadata empty
    # — so an `id` assertion over hand-appended rows only ever exercises the fallback, and
    # a misspelled lookup would ship uuid/`seq-N` drift against the snapshot with the test
    # still green. These rows go in through `annotate_and_append`, which stamps one.
    assert stamped and all(stamped), "the append path stopped stamping event ids"
    assert [e.get("id") for e in events] == stamped, (
        f"frame ids do not match what the log stamped: {[e.get('id') for e in events]} vs {stamped}"
    )

    # The spelling is `SessionEvent`'s, not the snapshot transcript item's. The snapshot
    # emits `toolCalls`/`toolCallId`/`toolName` and a client maps those to this shape via
    # `snapshotToEvents` before folding; the incremental frame is already in the folded
    # shape and skips that step. Asserted so the divergence is a decision on the record
    # rather than something a future reader has to guess at.
    assert "toolCalls" not in assistant and "toolCallId" not in tool


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
    # Exactly the turn, exactly once. `"42" in contents` would also pass a drain that
    # discarded the cursor `_drain_session_events` hands back and re-emitted every event
    # on every poll — a live failure mode, since the cursor crosses a return value.
    assert contents == ["", "42"], (
        f"expected the staged turn once: lost between the cursor read and the first poll,"
        f" or delivered more than once: {contents}"
    )
    assert "old answer" not in contents, f"the stream replayed history it should have skipped: {contents}"


@pytest.mark.asyncio
async def test_the_cursor_is_the_log_sequence_not_a_frame_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`id:` is the *next session sequence*, the same meaning it has on the reattach
    stream, so a client can hand it straight back as `Last-Event-ID`.

    The thread is seeded first on purpose. On an empty thread the log sequence and the
    frame ordinal are the same numbers, so the obvious wrong implementation — a
    per-connection counter, which is what most SSE code does — satisfies every ordering
    assertion and even `ids[-1] == seqs[-1] + 1`. Seeding makes the two diverge, and
    pairing each id with its own event's `seq` is what actually pins the meaning.
    """
    settings = _settings("tail-cursor")
    thread = "default:cursor"
    _force_durable(monkeypatch)
    await _append(
        settings,
        thread,
        *(AppendableEvent(kind="message", role="user", content=f"old{i}") for i in range(4)),
    )

    async def worker_ran_a_turn() -> None:
        await _append(settings, thread, *_turn())

    _stub_fiber(monkeypatch, on_poll=[worker_ran_a_turn], statuses=["running"])

    async with _client(settings) as client:
        body = await _post_stream(client, "cursor")

    frames = [(i, p["data"]) for i, p in _blocks(body) if p.get("event") == "session_event"]
    ids = [i for i, _ in frames]
    assert len(ids) == 2, f"expected one frame per appended event, got {ids}"
    assert ids == [e["seq"] + 1 for _, e in frames], f"an id is not its own event's next sequence: {frames}"
    assert min(ids) > 4, f"a frame counter would start at 1 here; the log is past 4: {ids}"
    assert ids == sorted(ids) and len(set(ids)) == len(ids), f"cursor went backwards or repeated: {ids}"

    seqs = [e.seq for e in await _session(settings, thread).get_events()]
    assert ids[-1] == seqs[-1] + 1, "the last id is not the next sequence the client should ask for"


@pytest.mark.asyncio
async def test_the_cursor_a_durable_stream_hands_back_resumes_without_replaying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ "Resumable" is the claim; this is the round trip that makes it one.

    Take the last `id:` off the durable stream, hand it to `GET /chat/stream/{thread}` as
    `Last-Event-ID`, and the reattach must start *after* what the durable stream already
    delivered — no snapshot, no replay — while still carrying anything that landed since.
    """
    settings = _settings("tail-resume")
    thread = "default:resume"
    _force_durable(monkeypatch)

    async def worker_ran_a_turn() -> None:
        await _append(settings, thread, *_turn())

    _stub_fiber(monkeypatch, on_poll=[worker_ran_a_turn], statuses=["running"])

    async with _client(settings) as client:
        body = await _post_stream(client, "resume")
        last_id = [i for i, p in _blocks(body) if p.get("event") == "session_event"][-1]
        # Something lands after the durable stream let go.
        await _append(settings, thread, AppendableEvent(kind="message", role="assistant", content="later"))
        resumed = ""
        async with client.stream(
            "GET", "/chat/stream/resume", headers={"last-event-id": str(last_id)}
        ) as resp:
            assert resp.status_code == 200, (resp.status_code, await resp.aread())
            async for chunk in resp.aiter_text():
                resumed += chunk

    names = _names(resumed)
    assert "snapshot" not in names, f"a warm reattach should not re-send the transcript: {names}"
    contents = [p["data"]["content"] for _, p in _blocks(resumed) if p.get("event") == "session_event"]
    assert contents == ["later"], f"the reattach replayed or skipped across the handoff: {contents}"


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


@pytest.mark.asyncio
async def test_a_failed_run_still_delivers_the_transcript_it_got_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The drain runs before the terminal check for *every* terminal status, not only
    `completed` — and a failure is the case a user most wants the transcript for."""
    settings = _settings("tail-failed")
    thread = "default:failed"
    _force_durable(monkeypatch)

    async def worker_got_partway() -> None:
        await _append(settings, thread, *_turn())

    _stub_fiber(monkeypatch, on_poll=[worker_got_partway], statuses=["failed"], error="model_unavailable")

    async with _client(settings) as client:
        body = await _post_stream(client, "failed")

    names = _names(body)
    assert "session_event" in names, f"a failed run dropped the work it did do: {names}"
    assert "event: error" in body, "a failed run closed the stream with no error frame"
    assert "model_unavailable" in body
    assert body.index("session_event") < body.index("model_unavailable"), (
        "the failure was reported before the work that led to it"
    )


@pytest.mark.asyncio
async def test_an_expired_run_delivers_its_events_before_saying_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expiry ends the stream, but not before what already landed goes out."""
    settings = _settings("tail-expired")
    thread = "default:expired"
    _force_durable(monkeypatch)

    async def worker_appended() -> None:
        await _append(settings, thread, *_turn())

    _stub_fiber(
        monkeypatch,
        on_poll=[worker_appended],
        statuses=["running", "running"],
        expires_at=1,  # already past
    )

    async with _client(settings) as client:
        body = await _post_stream(client, "expired")

    names = _names(body)
    assert "session_event" in names, f"expiry discarded the transcript: {names}"
    assert "run_expired" in body, f"the stream closed without saying it expired: {body[-300:]}"
    assert body.index("session_event") < body.index("run_expired"), names


@pytest.mark.asyncio
async def test_a_failing_log_read_degrades_to_status_only_and_keeps_its_place(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read that raises must not fail the run stream, and must not lose its place.

    The answer still arrives on `final`, which is all this endpoint promised before the
    tail existed. And because the cursor is left untouched on failure, the next poll
    re-reads the range that failed rather than skipping past it — so the events arrive
    late rather than never. Both halves are `except` branches, which are exactly the
    controls that look present and do nothing until something proves otherwise.
    """
    settings = _settings("tail-raises")
    thread = "default:raises"
    _force_durable(monkeypatch)

    store = get_session_store(settings, tenant_id="default")
    session = store.open(thread)
    real_get = session.get_events
    calls = {"n": 0}

    async def _flaky_get_events(*a: Any, **k: Any) -> Any:
        # Only the tail's reads, which pass a `GetEventsOpts`. `_append`'s own read-back of
        # stamped ids goes through the same session with no arguments, and failing that
        # would break the fixture rather than the thing under test.
        if not a and not k:
            return await real_get()
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("session store down")
        return await real_get(*a, **k)

    monkeypatch.setattr(session, "get_events", _flaky_get_events)

    async def worker_appended() -> None:
        await _append(settings, thread, *_turn())

    # Appended after the cursor was captured, so the first read *should* see it — and that
    # first read is the one that raises. Whether the events arrive on the second poll is
    # exactly the question of whether the cursor survived the failure.
    _stub_fiber(monkeypatch, on_start=worker_appended, statuses=["running", "running"])

    async with _client(settings) as client:
        body = await _post_stream(client, "raises")

    assert calls["n"] >= 2, f"the stream gave up after one failed read: {calls}"
    contents = [p["data"]["content"] for _, p in _blocks(body) if p.get("event") == "session_event"]
    assert contents == ["", "42"], f"the failed read lost its place rather than retrying: {contents}"
    assert "final" in _names(body), "a failing tail took the answer down with it"


@pytest.mark.asyncio
async def test_many_events_in_one_poll_arrive_in_order_with_contiguous_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ordering, pinned rather than inferred: every other test drains exactly two."""
    settings = _settings("tail-batch")
    thread = "default:batch"
    _force_durable(monkeypatch)

    async def worker_ran_five() -> None:
        await _append(
            settings,
            thread,
            *(AppendableEvent(kind="message", role="assistant", content=f"m{i}") for i in range(5)),
        )

    _stub_fiber(monkeypatch, on_poll=[worker_ran_five], statuses=["running"])

    async with _client(settings) as client:
        body = await _post_stream(client, "batch")

    frames = [(i, p["data"]) for i, p in _blocks(body) if p.get("event") == "session_event"]
    assert [e["content"] for _, e in frames] == [f"m{i}" for i in range(5)], frames
    ids = [i for i, _ in frames]
    assert ids == list(range(ids[0], ids[0] + 5)), f"ids are not contiguous across one drain: {ids}"


@pytest.mark.asyncio
async def test_the_accepted_shape_matches_what_the_real_start_returns() -> None:
    """The fixture above describes a run; this is what stops it describing a fiction.

    `_durable_tail_reader` reads `thread_id`, `fiber_id` and `resume_token` off whatever
    `start_durable_chat` returned, and every other test in this file gets those keys from a
    stub. The fiber store has a real `memory://` twin, so the contract can be checked
    against the real function rather than asserted twice in two divergent fixtures.
    """
    from felix.durability.fibers import fiber_thread_id
    from felix.durability.runs import get_durable_run, start_durable_chat
    from felix.manifests.schema import ExecutionSpec
    from felix.patterns.types import ChatMessage

    settings = _settings("tail-contract")
    accepted = await start_durable_chat(
        settings,
        "default",
        manifest_id="quick",
        messages=[ChatMessage(role="user", content="hi")],
        thread_id=None,
        model_id=None,
        execution=ExecutionSpec(mode="durable"),
    )

    assert {"resume_token", "fiber_id", "expires_at", "thread_id"} <= set(accepted)
    assert accepted["thread_id"] is None, "a run started with no thread should echo none"
    # The derivation the API makes from these keys has to name a thread the worker will
    # actually write to. `fibers.py` builds the same id from the same helper.
    assert fiber_thread_id("default", str(accepted["fiber_id"])).startswith("default:fiber:")
    assert accepted["fiber_id"] == accepted["resume_token"], (
        "the API derives the fiber thread from `fiber_id`, falling back to `resume_token`"
    )

    run = await get_durable_run(settings, "default", str(accepted["resume_token"]))
    assert run is not None and {"status", "final", "error"} <= set(run)


# --- what the run is blocked on ---------------------------------------------------------


async def _gate(settings: Settings, thread: str, **kw: Any) -> dict[str, Any]:
    """A pending approval on `thread`, written the way the governance wrapper writes one."""
    from felix.approvals.store import create_pending

    return await create_pending(
        settings,
        "default",
        tool_name=kw.pop("tool_name", "write_file"),
        call_signature=kw.pop("call_signature", "sig-1"),
        manifest_id="quick",
        args=kw.pop("args", {"path": "notes.txt"}),
        thread_id=thread,
        **kw,
    )


@pytest.mark.asyncio
async def test_a_durable_run_announces_what_it_is_blocked_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gap the session-log tail cannot close.

    `_append_produced` writes assistant turns and tool results; a request for permission is
    neither, so no amount of tailing the transcript reaches it — on exactly the path where a
    human has time to answer, because the agent is in the worker and nothing it emits can
    cross to the API's stream. The approvals table is the durable record that can, and the
    frame is rebuilt from a row rather than forwarded.
    """
    settings = _settings("tail-approval")
    thread = "default:gated"
    _force_durable(monkeypatch)

    async def worker_hit_a_gate() -> None:
        await _gate(
            settings,
            thread,
            rule_id="workspace-write",
            reason="writes outside the workspace need a human",
            tool_call_id="call_7",
            ttl_seconds=300,
        )

    _stub_fiber(monkeypatch, on_poll=[worker_hit_a_gate], statuses=["running"])

    async with _client(settings) as client:
        body = await _post_stream(client, "gated")

    gates = [p["data"] for _, p in _blocks(body) if p.get("event") == "approval_required"]
    assert gates, f"a blocked durable run announced nothing: {_names(body)}"
    (gate,) = gates
    assert gate["tool_name"] == "write_file"
    assert gate["args"] == {"path": "notes.txt"}
    assert gate["rule_id"] == "workspace-write"
    # The two fields felix#245 put on the row so the frame could be rebuilt rather than
    # forwarded. Without them this path could announce a gate but not say why, which is the
    # state the poll was already in.
    assert gate["reason"] == "writes outside the workspace need a human"
    assert gate["tool_call_id"] == "call_7"
    assert isinstance(gate["expires_at"], int)
    assert gate["thread_id"] == thread


@pytest.mark.asyncio
async def test_a_pending_approval_is_announced_once_however_many_polls_see_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`list_approvals` answers "what is pending", not "what is new".

    So the same row comes back on every poll until someone decides it, and without a dedupe
    the client is re-shown a prompt it may already have answered — worse than being shown it
    late. There is no cursor to lean on the way the transcript has one.
    """
    settings = _settings("tail-approval-once")
    thread = "default:gated-once"
    _force_durable(monkeypatch)
    await _gate(settings, thread, ttl_seconds=300)

    # Four polls over one unchanging pending row.
    _stub_fiber(monkeypatch, statuses=["running", "running", "running"])

    async with _client(settings) as client:
        body = await _post_stream(client, "gated-once")

    gates = [p for _, p in _blocks(body) if p.get("event") == "approval_required"]
    assert len(gates) == 1, f"the prompt was re-announced on every poll: {len(gates)} frames"


@pytest.mark.asyncio
async def test_another_threads_approval_is_not_announced(monkeypatch: pytest.MonkeyPatch) -> None:
    """Thread-scoped, and it has to be: `GET /approvals` is tenant-wide.

    Announcing every pending approval in the tenant on one run's stream would leak the tool
    names and arguments of other conversations to whoever started this one.
    """
    settings = _settings("tail-approval-scope")
    _force_durable(monkeypatch)
    await _gate(settings, "default:theirs", call_signature="sig-theirs", ttl_seconds=300)

    _stub_fiber(monkeypatch, statuses=["running", "running"])

    async with _client(settings) as client:
        body = await _post_stream(client, "mine")

    gates = [p for _, p in _blocks(body) if p.get("event") == "approval_required"]
    assert gates == [], f"another thread's approval reached this run's stream: {gates}"


@pytest.mark.asyncio
async def test_an_approval_decided_before_the_stream_opened_is_not_announced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only `status="pending"` is a question. A decided row is history, and re-announcing it
    would put a prompt on screen for a call that is already running or already refused."""
    from felix.approvals.store import decide

    settings = _settings("tail-approval-decided")
    thread = "default:decided"
    _force_durable(monkeypatch)
    row = await _gate(settings, thread, ttl_seconds=300)
    await decide(settings, "default", row["id"], decision="approved", decided_by="operator")

    _stub_fiber(monkeypatch, statuses=["running", "running"])

    async with _client(settings) as client:
        body = await _post_stream(client, "decided")

    gates = [p for _, p in _blocks(body) if p.get("event") == "approval_required"]
    assert gates == [], f"a decided approval was announced as if it were still waiting: {gates}"


@pytest.mark.asyncio
async def test_a_chat_scoped_caller_is_not_told_what_the_run_is_blocked_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`POST /chat/stream` must not route around the `approvals:read` scope.

    `thread_id` comes from the request body, so the thread a durable run names is a question
    the caller *chose*, not one they own — nothing in Felix binds a thread to a principal.
    Ungated, a caller with chat access and without `approvals:read` could name any thread in
    the tenant and read the tool names, full arguments and gate reasons it is blocked on:
    exactly the payload `GET /approvals` refuses them.

    Every other test in this file runs under `auth_mode="none"`, where the check is a no-op
    by design — so this one turns auth on, and it is the only place the gate can fail.
    """
    settings = Settings(
        allow_insecure=True,
        auth_mode="api_key",
        environment="development",
        object_store="memory",
        database_url="memory://tail-scope",
        redis_url="",
        auth_api_keys=json.dumps(
            {
                "sk-chat": {"tenant_id": "default", "sub": "chat", "scopes": ["chat"]},
                "sk-ops": {"tenant_id": "default", "sub": "ops", "scopes": ["approvals:read"]},
            }
        ),
        stream_resume_poll_seconds=0.1,
        stream_resume_poll_max_seconds=0.1,
        stream_resume_idle_seconds=0.2,
    )
    thread = "default:scoped"
    _force_durable(monkeypatch)
    await _gate(settings, thread, reason="wire transfers need a human", ttl_seconds=300)
    _stub_fiber(monkeypatch, statuses=["running", "running"])

    async def _stream(token: str) -> str:
        body = ""
        async with (
            _client(settings) as client,
            client.stream(
                "POST",
                "/chat/stream",
                json={
                    "manifest": "quick",
                    "thread_id": "scoped",
                    "messages": [{"role": "user", "content": "hi"}],
                },
                headers={"Authorization": f"Bearer {token}"},
            ) as resp,
        ):
            assert resp.status_code == 200, (resp.status_code, await resp.aread())
            async for chunk in resp.aiter_text():
                body += chunk
        return body

    denied = _names(await _stream("sk-chat"))
    assert "approval_required" not in denied, (
        "a caller without approvals:read read the gate through the chat stream"
    )
    # The transcript and the answer are untouched — the scope gates the announcement, not
    # the run. Without this half, deleting the whole feature would also pass.
    assert "run_status" in denied and "final" in denied

    _stub_fiber(monkeypatch, statuses=["running", "running"])
    allowed = _names(await _stream("sk-ops"))
    assert "approval_required" in allowed, (
        "the scope that grants this on /approvals does not grant it on the stream"
    )


@pytest.mark.asyncio
async def test_an_unexpected_failure_closes_the_stream_with_an_error_not_a_truncation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raise mid-body must not end the response silently.

    The status read and the tail both degrade on their own, so this is about everything
    else: under an already-sent `200 OK`, an unhandled exception ends the body with no
    `event: error` and no `[DONE]`, and a client cannot tell a truncated connection from a
    finished one. `resume_stream_gen` has guarded this since it was written; the durable loop
    did not, which was an asymmetry rather than a decision.
    """
    settings = _settings("tail-boom")
    _force_durable(monkeypatch)
    _stub_fiber(monkeypatch, statuses=["running"])

    import felix_api.routes._streaming as streaming_mod

    def _boom(*a: Any, **k: Any) -> str:
        raise RuntimeError("frame builder exploded")

    monkeypatch.setattr(streaming_mod, "frame", _boom)

    async with _client(settings) as client:
        body = await _post_stream(client, "boom")

    assert "event: error" in body, f"the stream ended without saying it failed: {body[-200:]}"
    assert body.rstrip().endswith("[DONE]"), "the stream ended without terminating the protocol"
    # The message is client-safe, not a traceback.
    assert "Traceback" not in body


@pytest.mark.asyncio
async def test_a_failing_approvals_read_does_not_take_the_run_stream_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Degrade to transcript-and-status, the same way a failed session read degrades.

    The answer still arrives on `final`, which is everything this endpoint promised before
    either tail existed.
    """
    settings = _settings("tail-approval-raises")
    thread = "default:appr-raises"
    _force_durable(monkeypatch)
    await _gate(settings, thread, ttl_seconds=300)

    from felix.approvals import store as approvals_store

    async def _boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("approvals store down")

    monkeypatch.setattr(approvals_store, "list_approvals", _boom)

    async def worker_ran_a_turn() -> None:
        await _append(settings, thread, *_turn())

    _stub_fiber(monkeypatch, on_poll=[worker_ran_a_turn], statuses=["running"])

    async with _client(settings) as client:
        body = await _post_stream(client, "appr-raises")

    names = _names(body)
    assert "approval_required" not in names
    assert "session_event" in names, "a failing approvals read took the transcript with it"
    assert "final" in names, "a failing approvals read took the answer with it"
