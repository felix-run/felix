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

from tests.support.durable_stream import (
    durable_settings,
    force_durable,
    pending_gate,
    post_stream,
    stub_fiber,
)
from tests.support.factories import app_client
from tests.support.sse import sse_blocks, sse_event_names


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


@pytest.mark.asyncio
async def test_a_durable_run_streams_its_tool_calls_not_only_its_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The defect itself: the answer arrived and the work behind it did not."""
    settings = durable_settings()
    thread = "default:tools"
    force_durable(monkeypatch)

    async def worker_ran_a_turn() -> None:
        await _append(settings, thread, *_turn())

    stub_fiber(monkeypatch, on_poll=[None, worker_ran_a_turn], statuses=["running", "running"])

    async with app_client(settings) as client:
        body = await post_stream(client, "tools")

    names = sse_event_names(body)
    assert "session_event" in names, f"a durable run reported no progress at all: {names}"
    # Interleaved, which is the claim. Only the tailed half is new, so it is the half that
    # gets asserted everywhere else — this pins that the status frames it interleaves with
    # did not stop arriving.
    first = names.index("session_event")
    assert "run_status" in names[:first] and "run_status" in names[first:], (
        f"progress did not land between status frames: {names}"
    )
    tool = [
        p for _, p in sse_blocks(body) if p.get("event") == "session_event" and p["data"]["role"] == "tool"
    ]
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
    settings = durable_settings()
    thread = "default:cards"
    force_durable(monkeypatch)

    stamped: list[str] = []

    async def worker_ran_a_turn() -> None:
        stamped.extend(await _append(settings, thread, *_turn()))

    stub_fiber(monkeypatch, on_poll=[worker_ran_a_turn], statuses=["running"])

    async with app_client(settings) as client:
        body = await post_stream(client, "cards")

    events = [p["data"] for _, p in sse_blocks(body) if p.get("event") == "session_event"]
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
async def test_a_session_event_carries_the_reasoning_a_person_can_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A durable run's reasoning reaches a watching client while the run is going.

    `chat_message_to_event` keeps the provider's reasoning blocks on the assistant row as
    `metadata.thinking`, and the snapshot sends them. The tail frame did not, so on a
    durable run — which streams no deltas at all — reasoning appeared only after the run
    had landed and the client re-read the snapshot. The frame now carries the readable
    blocks, and only those: a `signature` and a `redacted_thinking` block exist to be
    replayed to the provider, and neither belongs in front of a person.
    """
    settings = durable_settings()
    thread = "default:thinking"
    force_durable(monkeypatch)

    async def worker_thought_then_answered() -> None:
        await _append(
            settings,
            thread,
            AppendableEvent(
                kind="message",
                role="assistant",
                content="the answer",
                metadata={
                    "thinking": [
                        {"type": "thinking", "thinking": "weigh the options", "signature": "sig-abc"},
                        {"type": "redacted_thinking", "data": "opaque-xyz"},
                        {"type": "thinking", "thinking": "   ", "signature": "sig-empty"},
                    ]
                },
            ),
            AppendableEvent(kind="message", role="assistant", content="no reasoning here"),
        )

    stub_fiber(monkeypatch, on_poll=[worker_thought_then_answered], statuses=["running"])

    async with app_client(settings) as client:
        body = await post_stream(client, "thinking")

    events = [p["data"] for _, p in sse_blocks(body) if p.get("event") == "session_event"]
    thought = next(e for e in events if e["content"] == "the answer")
    plain = next(e for e in events if e["content"] == "no reasoning here")

    assert thought.get("metadata") == {"thinking": [{"type": "thinking", "thinking": "weigh the options"}]}
    # Opaque on purpose, and replay-only: never on the frame.
    assert "sig-abc" not in body and "opaque-xyz" not in body
    # Nothing readable means no key at all, the way `tool_calls` is omitted when empty.
    assert "metadata" not in plain


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
    settings = durable_settings()
    thread = "default:race"
    force_durable(monkeypatch)
    # History from an earlier turn. The cursor exists so this is *not* replayed.
    await _append(settings, thread, AppendableEvent(kind="message", role="assistant", content="old answer"))

    async def worker_beat_the_stream() -> None:
        await _append(settings, thread, *_turn())

    stub_fiber(monkeypatch, on_start=worker_beat_the_stream, statuses=["running"])

    async with app_client(settings) as client:
        body = await post_stream(client, "race")

    contents = [p["data"]["content"] for _, p in sse_blocks(body) if p.get("event") == "session_event"]
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
    settings = durable_settings()
    thread = "default:cursor"
    force_durable(monkeypatch)
    await _append(
        settings,
        thread,
        *(AppendableEvent(kind="message", role="user", content=f"old{i}") for i in range(4)),
    )

    async def worker_ran_a_turn() -> None:
        await _append(settings, thread, *_turn())

    stub_fiber(monkeypatch, on_poll=[worker_ran_a_turn], statuses=["running"])

    async with app_client(settings) as client:
        body = await post_stream(client, "cursor")

    frames = [(i, p["data"]) for i, p in sse_blocks(body) if p.get("event") == "session_event"]
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
    settings = durable_settings()
    thread = "default:resume"
    force_durable(monkeypatch)

    async def worker_ran_a_turn() -> None:
        await _append(settings, thread, *_turn())

    stub_fiber(monkeypatch, on_poll=[worker_ran_a_turn], statuses=["running"])

    async with app_client(settings) as client:
        body = await post_stream(client, "resume")
        last_id = [i for i, p in sse_blocks(body) if p.get("event") == "session_event"][-1]
        # Something lands after the durable stream let go.
        await _append(settings, thread, AppendableEvent(kind="message", role="assistant", content="later"))
        resumed = ""
        async with client.stream(
            "GET", "/chat/stream/resume", headers={"last-event-id": str(last_id)}
        ) as resp:
            assert resp.status_code == 200, (resp.status_code, await resp.aread())
            async for chunk in resp.aiter_text():
                resumed += chunk

    names = sse_event_names(resumed)
    assert "snapshot" not in names, f"a warm reattach should not re-send the transcript: {names}"
    contents = [p["data"]["content"] for _, p in sse_blocks(resumed) if p.get("event") == "session_event"]
    assert contents == ["later"], f"the reattach replayed or skipped across the handoff: {contents}"


@pytest.mark.asyncio
async def test_a_run_with_no_thread_tails_the_thread_the_fiber_mints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run started without a `thread_id` is not a run without a thread: the fiber writes
    its transcript to `{tenant}:fiber:{id}` just the same."""
    from felix.durability.fibers import fiber_thread_id

    settings = durable_settings()
    force_durable(monkeypatch)
    thread = fiber_thread_id("default", "fiber-1")

    async def worker_ran_a_turn() -> None:
        await _append(settings, thread, *_turn())

    seen = stub_fiber(monkeypatch, on_poll=[worker_ran_a_turn], statuses=["running"])

    async with app_client(settings) as client:
        body = await post_stream(client, None)

    assert seen["thread_id"] is None, "the request was supposed to carry no thread"
    contents = [p["data"]["content"] for _, p in sse_blocks(body) if p.get("event") == "session_event"]
    assert "42" in contents, f"an anonymous durable run reported no progress: {contents}"


@pytest.mark.asyncio
async def test_a_turn_appended_just_before_completion_still_precedes_the_final_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fiber writes the transcript and *then* saves `completed`, so the iteration that
    sees the terminal status must drain before it emits `final` — otherwise the last turn
    is lost to the client that was watching it happen."""
    settings = durable_settings()
    thread = "default:last"
    force_durable(monkeypatch)

    async def worker_finished() -> None:
        await _append(settings, thread, *_turn())

    stub_fiber(monkeypatch, on_poll=[worker_finished], statuses=["completed"])

    async with app_client(settings) as client:
        body = await post_stream(client, "last")

    names = sse_event_names(body)
    assert "session_event" in names, f"the last turn never reached the client: {names}"
    assert names.index("session_event") < names.index("final"), names
    assert body.rstrip().endswith("[DONE]")


@pytest.mark.asyncio
async def test_a_failed_run_still_delivers_the_transcript_it_got_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The drain runs before the terminal check for *every* terminal status, not only
    `completed` — and a failure is the case a user most wants the transcript for."""
    settings = durable_settings()
    thread = "default:failed"
    force_durable(monkeypatch)

    async def worker_got_partway() -> None:
        await _append(settings, thread, *_turn())

    stub_fiber(monkeypatch, on_poll=[worker_got_partway], statuses=["failed"], error="model_unavailable")

    async with app_client(settings) as client:
        body = await post_stream(client, "failed")

    names = sse_event_names(body)
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
    settings = durable_settings()
    thread = "default:expired"
    force_durable(monkeypatch)

    async def worker_appended() -> None:
        await _append(settings, thread, *_turn())

    stub_fiber(
        monkeypatch,
        on_poll=[worker_appended],
        statuses=["running", "running"],
        expires_at=1,  # already past
    )

    async with app_client(settings) as client:
        body = await post_stream(client, "expired")

    names = sse_event_names(body)
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
    settings = durable_settings()
    thread = "default:raises"
    force_durable(monkeypatch)

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
    stub_fiber(monkeypatch, on_start=worker_appended, statuses=["running", "running"])

    async with app_client(settings) as client:
        body = await post_stream(client, "raises")

    assert calls["n"] >= 2, f"the stream gave up after one failed read: {calls}"
    contents = [p["data"]["content"] for _, p in sse_blocks(body) if p.get("event") == "session_event"]
    assert contents == ["", "42"], f"the failed read lost its place rather than retrying: {contents}"
    assert "final" in sse_event_names(body), "a failing tail took the answer down with it"


@pytest.mark.asyncio
async def test_many_events_in_one_poll_arrive_in_order_with_contiguous_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ordering, pinned rather than inferred: every other test drains exactly two."""
    settings = durable_settings()
    thread = "default:batch"
    force_durable(monkeypatch)

    async def worker_ran_five() -> None:
        await _append(
            settings,
            thread,
            *(AppendableEvent(kind="message", role="assistant", content=f"m{i}") for i in range(5)),
        )

    stub_fiber(monkeypatch, on_poll=[worker_ran_five], statuses=["running"])

    async with app_client(settings) as client:
        body = await post_stream(client, "batch")

    frames = [(i, p["data"]) for i, p in sse_blocks(body) if p.get("event") == "session_event"]
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

    settings = durable_settings()
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


@pytest.mark.asyncio
async def test_a_durable_run_announces_what_it_is_blocked_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gap the session-log tail cannot close.

    `_append_produced` writes assistant turns and tool results; a request for permission is
    neither, so no amount of tailing the transcript reaches it — on exactly the path where a
    human has time to answer, because the agent is in the worker and nothing it emits can
    cross to the API's stream. The approvals table is the durable record that can, and the
    frame is rebuilt from a row rather than forwarded.
    """
    settings = durable_settings()
    thread = "default:gated"
    force_durable(monkeypatch)

    async def worker_hit_a_gate() -> None:
        await pending_gate(
            settings,
            thread,
            rule_id="workspace-write",
            reason="writes outside the workspace need a human",
            tool_call_id="call_7",
            ttl_seconds=300,
        )

    stub_fiber(monkeypatch, on_poll=[worker_hit_a_gate], statuses=["running"])

    async with app_client(settings) as client:
        body = await post_stream(client, "gated")

    gates = [p["data"] for _, p in sse_blocks(body) if p.get("event") == "approval_required"]
    assert gates, f"a blocked durable run announced nothing: {sse_event_names(body)}"
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
    settings = durable_settings()
    thread = "default:gated-once"
    force_durable(monkeypatch)
    await pending_gate(settings, thread, ttl_seconds=300)

    # Four polls over one unchanging pending row.
    stub_fiber(monkeypatch, statuses=["running", "running", "running"])

    async with app_client(settings) as client:
        body = await post_stream(client, "gated-once")

    gates = [p for _, p in sse_blocks(body) if p.get("event") == "approval_required"]
    assert len(gates) == 1, f"the prompt was re-announced on every poll: {len(gates)} frames"


@pytest.mark.asyncio
async def test_another_threads_approval_is_not_announced(monkeypatch: pytest.MonkeyPatch) -> None:
    """Thread-scoped, and it has to be: `GET /approvals` is tenant-wide.

    Announcing every pending approval in the tenant on one run's stream would leak the tool
    names and arguments of other conversations to whoever started this one.
    """
    settings = durable_settings()
    force_durable(monkeypatch)
    await pending_gate(settings, "default:theirs", call_signature="sig-theirs", ttl_seconds=300)

    stub_fiber(monkeypatch, statuses=["running", "running"])

    async with app_client(settings) as client:
        body = await post_stream(client, "mine")

    gates = [p for _, p in sse_blocks(body) if p.get("event") == "approval_required"]
    assert gates == [], f"another thread's approval reached this run's stream: {gates}"


@pytest.mark.asyncio
async def test_an_approval_decided_before_the_stream_opened_is_not_announced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only `status="pending"` is a question. A decided row is history, and re-announcing it
    would put a prompt on screen for a call that is already running or already refused."""
    from felix.approvals.store import decide

    settings = durable_settings()
    thread = "default:decided"
    force_durable(monkeypatch)
    row = await pending_gate(settings, thread, ttl_seconds=300)
    await decide(settings, "default", row["id"], decision="approved", decided_by="operator")

    stub_fiber(monkeypatch, statuses=["running", "running"])

    async with app_client(settings) as client:
        body = await post_stream(client, "decided")

    gates = [p for _, p in sse_blocks(body) if p.get("event") == "approval_required"]
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
    force_durable(monkeypatch)
    await pending_gate(settings, thread, reason="wire transfers need a human", ttl_seconds=300)
    stub_fiber(monkeypatch, statuses=["running", "running"])

    async def _stream(token: str) -> str:
        body = ""
        async with (
            app_client(settings) as client,
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

    denied = sse_event_names(await _stream("sk-chat"))
    assert "approval_required" not in denied, (
        "a caller without approvals:read read the gate through the chat stream"
    )
    # The transcript and the answer are untouched — the scope gates the announcement, not
    # the run. Without this half, deleting the whole feature would also pass.
    assert "run_status" in denied and "final" in denied

    stub_fiber(monkeypatch, statuses=["running", "running"])
    allowed = sse_event_names(await _stream("sk-ops"))
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
    settings = durable_settings()
    force_durable(monkeypatch)
    stub_fiber(monkeypatch, statuses=["running"])

    import felix_api.routes._streaming as streaming_mod

    def _boom(*a: Any, **k: Any) -> str:
        raise RuntimeError("frame builder exploded")

    monkeypatch.setattr(streaming_mod, "frame", _boom)

    async with app_client(settings) as client:
        body = await post_stream(client, "boom")

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
    settings = durable_settings()
    thread = "default:appr-raises"
    force_durable(monkeypatch)
    await pending_gate(settings, thread, ttl_seconds=300)

    from felix.approvals import store as approvals_store

    async def _boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("approvals store down")

    monkeypatch.setattr(approvals_store, "list_approvals", _boom)

    async def worker_ran_a_turn() -> None:
        await _append(settings, thread, *_turn())

    stub_fiber(monkeypatch, on_poll=[worker_ran_a_turn], statuses=["running"])

    async with app_client(settings) as client:
        body = await post_stream(client, "appr-raises")

    names = sse_event_names(body)
    assert "approval_required" not in names
    assert "session_event" in names, "a failing approvals read took the transcript with it"
    assert "final" in names, "a failing approvals read took the answer with it"


# --- what the run is waiting on its client for ------------------------------------------


@pytest.mark.asyncio
async def test_a_durable_run_asks_its_client_to_run_a_client_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    """The client-tool half of the gap the approvals relay closed.

    A client tool announces itself with a side event, which reaches only a stream in the
    agent's process; a durable run's agent is in the worker. Without this, `cowork`'s
    `local_shell` waited out its timeout on every durable run with no client ever asked.
    """
    from felix.tools import client_requests

    settings = durable_settings()
    thread = "default:client-tool"
    force_durable(monkeypatch)
    request = {
        "id": "call_ls",
        "name": "local_shell",
        "args": {"command": "ls"},
        "thread_id": thread,
        "transport": "client",
    }

    async def worker_called_a_client_tool() -> None:
        await client_requests.record(thread, request, timeout=300)

    stub_fiber(monkeypatch, on_poll=[worker_called_a_client_tool], statuses=["running", "running", "running"])
    try:
        async with app_client(settings) as client:
            body = await post_stream(client, "client-tool")
    finally:
        await client_requests.clear(thread, "call_ls")

    asked = [p["data"] for _, p in sse_blocks(body) if p.get("event") == "tool_request"]
    # Once, however many polls saw it pending, and exactly the payload the side event carries,
    # so a client answers it with the handler it already has.
    assert asked == [request], f"expected one tool_request, got {asked} in {sse_event_names(body)}"


@pytest.mark.asyncio
async def test_another_threads_client_tool_is_not_announced(monkeypatch: pytest.MonkeyPatch) -> None:
    from felix.tools import client_requests

    settings = durable_settings()
    force_durable(monkeypatch)
    await client_requests.record("default:theirs", {"id": "call_x", "name": "local_shell"}, timeout=300)
    stub_fiber(monkeypatch, statuses=["running", "running"])
    try:
        async with app_client(settings) as client:
            body = await post_stream(client, "mine-client")
    finally:
        await client_requests.clear("default:theirs", "call_x")

    assert "tool_request" not in sse_event_names(body), "another thread's client tool reached this stream"


def _spy_watch(monkeypatch: pytest.MonkeyPatch, *, delivering: bool) -> list[float]:
    """Replace the stream's watch with one that records each wait and never sleeps."""
    import contextlib

    from felix.session.notify import Wake
    from felix_api.routes import _streaming as streaming_mod

    slept: list[float] = []

    class _Watch:
        async def wait(self, *, timeout: float) -> Wake:
            slept.append(timeout)
            return Wake(woken=False, by_notification=delivering)

    @contextlib.asynccontextmanager
    async def _watch(_tenant: str, _thread: str):
        yield _Watch()

    monkeypatch.setattr(streaming_mod, "thread_watch", _watch)
    return slept


@pytest.mark.asyncio
async def test_a_notified_durable_stream_relaxes_past_the_short_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The run's status and its gates announce on the thread now, so a delivered wake covers
    everything this loop polls, and the poll relaxes to the long notified ceiling. It was pinned
    to `poll_max` (10 s) while they published nothing."""
    from felix_api.routes._streaming import NOTIFIED_POLL_CEILING_SECONDS

    settings = durable_settings()
    force_durable(monkeypatch)
    stub_fiber(monkeypatch, statuses=["running"] * 20)
    slept = _spy_watch(monkeypatch, delivering=True)

    async with app_client(settings) as client:
        body = await post_stream(client, "relaxes")

    assert "final" in sse_event_names(body)
    assert 10.0 < max(slept) <= NOTIFIED_POLL_CEILING_SECONDS, f"waits: {slept}"


@pytest.mark.asyncio
async def test_a_durable_stream_never_waits_past_the_runs_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expiry is a time nothing announces; with the long ceiling, a wait that ran past it would
    report the expired run a minute late."""
    import time

    settings = durable_settings()
    force_durable(monkeypatch)
    expires_at = int((time.time() + 3) * 1000)
    stub_fiber(monkeypatch, statuses=["running"] * 20, expires_at=expires_at)
    slept = _spy_watch(monkeypatch, delivering=True)

    async with app_client(settings) as client:
        await post_stream(client, "deadline")

    assert slept and max(slept) <= 3.0, f"waited past the deadline: {slept}"
