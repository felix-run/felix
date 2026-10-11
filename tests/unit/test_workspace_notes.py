"""An operator's direct edit to a workspace file reaches the agent — mid-run, without cancelling.

`/chat/sessions/custom` reaches only the *next* run (history is rendered once, at a run's start)
and a steer cancels the tool calls still to run. A workspace note is drained before every model
call, appended to the log as it is delivered, and never touches a tool batch. These drive the
queue and the react loop directly, with Redis unreachable — the fallback CI runs on.
"""

from __future__ import annotations

from typing import Any

from felix import workspace_notes
from felix.config import Settings
from felix.context import AuthContext, RequestContext, async_run_with_context
from felix.manifests.schema import ModelSpec
from felix.patterns.react import _ReactAgent
from felix.patterns.types import ChatMessage, InvokeInput, ToolCall
from felix.session.store import InMemorySessionStore
from felix.session.strategies import full_replay_session_strategy
from felix.tools.types import Tool, ToolInput, ToolInvocationCtx, ToolOutput
from felix.workspace_notes import WorkspaceNote, coalesce
from felix_ai.types import ModelChatResult, StreamDelta, TokenUsage

TENANT = "default"


# --- the note itself -----------------------------------------------------------------------


def test_a_write_names_the_path_and_the_size() -> None:
    text = WorkspaceNote(path="notes/plan.md", bytes=1204).text()
    assert text.startswith("The operator edited `notes/plan.md` directly in the workspace (now 1,204 bytes).")
    assert "read it again" in text


def test_a_write_without_a_size_says_none() -> None:
    assert "bytes" not in WorkspaceNote(path="a.txt").text()


def test_delete_and_rename_say_what_happened() -> None:
    assert "deleted `gone.md`" in WorkspaceNote(path="gone.md", op="delete").text()
    renamed = WorkspaceNote(path="old.md", op="rename", to_path="new.md").text()
    assert "renamed `old.md` to `new.md`" in renamed


def test_a_backtick_in_a_path_cannot_close_the_code_span() -> None:
    text = WorkspaceNote(path="a`b.md").text()
    assert "``a`b.md``" in text


def test_the_entry_metadata_is_structured_and_in_context() -> None:
    assert WorkspaceNote(path="p", op="write", bytes=3).metadata() == {
        "type": "workspace_edit",
        "path": "p",
        "op": "write",
        "bytes": 3,
        "source": "operator",
        "in_context": True,
    }
    assert WorkspaceNote(path="a", op="rename", to_path="b").metadata()["to_path"] == "b"


def test_a_folder_note_says_it_is_a_folder_and_how_many_files_went() -> None:
    one = WorkspaceNote(path="notes", op="delete", kind="folder", files=1).text()
    assert one.startswith("The operator deleted the folder `notes` (1 file) from the workspace.")
    many = WorkspaceNote(path="notes", op="delete", kind="folder", files=1204).text()
    assert "(1,204 files)" in many
    assert many.endswith("Do not recreate anything in it unless you are asked to.")
    moved = WorkspaceNote(path="a", op="rename", to_path="b/a", kind="folder").text()
    assert moved.startswith("The operator renamed the folder `a` to `b/a` in the workspace.")
    assert "paths under `a` are now under `b/a`; read them again before relying on" in moved


def test_a_folder_notes_metadata_and_frame_carry_its_kind_and_a_file_notes_do_not() -> None:
    gone = WorkspaceNote(path="d", op="delete", kind="folder", files=2)
    assert gone.metadata() == {
        "type": "workspace_edit",
        "path": "d",
        "op": "delete",
        "bytes": None,
        "source": "operator",
        "in_context": True,
        "kind": "folder",
        "files": 2,
    }
    assert gone.event_data() == {"path": "d", "op": "delete", "bytes": None, "kind": "folder", "files": 2}
    moved = WorkspaceNote(path="a", op="rename", to_path="b", kind="folder")
    assert moved.event_data() == {
        "path": "a",
        "op": "rename",
        "bytes": None,
        "to_path": "b",
        "kind": "folder",
    }
    assert "kind" not in WorkspaceNote(path="f", op="delete").metadata()
    assert "kind" not in WorkspaceNote(path="f", op="delete").event_data()


def test_a_folder_note_survives_the_queue() -> None:
    note = WorkspaceNote(path="d", op="delete", kind="folder", files=7)
    assert WorkspaceNote.from_json(note.to_json()) == note
    # A note queued by a replica from before folders existed reads back as a file note.
    old = '{"path": "a", "op": "delete", "bytes": null, "to_path": null}'
    assert WorkspaceNote.from_json(old) == WorkspaceNote(path="a", op="delete")


def test_coalescing_keeps_the_last_note_per_path_in_last_touched_order() -> None:
    notes = [
        WorkspaceNote(path="a", bytes=1),
        WorkspaceNote(path="b", bytes=2),
        WorkspaceNote(path="a", bytes=3),
        WorkspaceNote(path="b", op="delete"),
    ]
    assert coalesce(notes) == [WorkspaceNote(path="a", bytes=3), WorkspaceNote(path="b", op="delete")]


# --- the queue -----------------------------------------------------------------------------


async def test_nothing_is_queued_for_a_thread_with_no_run() -> None:
    thread = "default:wsn-idle"
    assert await workspace_notes.enqueue_if_running(TENANT, thread, WorkspaceNote(path="x")) is False
    assert await workspace_notes.drain(TENANT, thread) == []


async def test_a_running_thread_queues_and_a_drain_empties_it_coalesced() -> None:
    thread = "default:wsn-queue"
    await workspace_notes.mark_run_active(TENANT, thread)
    try:
        for size in (1, 2, 3):
            assert await workspace_notes.enqueue_if_running(
                TENANT, thread, WorkspaceNote(path="f", bytes=size)
            )
        assert await workspace_notes.drain(TENANT, thread) == [WorkspaceNote(path="f", bytes=3)]
        assert await workspace_notes.drain(TENANT, thread) == [], "a drained note was delivered twice"
    finally:
        await workspace_notes.mark_run_idle(TENANT, thread)
    assert await workspace_notes.run_active(TENANT, thread) is False


async def test_two_runs_on_one_thread_do_not_unmark_each_other() -> None:
    thread = "default:wsn-two"
    await workspace_notes.mark_run_active(TENANT, thread)
    await workspace_notes.mark_run_active(TENANT, thread)
    await workspace_notes.mark_run_idle(TENANT, thread)
    assert await workspace_notes.run_active(TENANT, thread) is True
    await workspace_notes.mark_run_idle(TENANT, thread)
    assert await workspace_notes.run_active(TENANT, thread) is False


# --- the loop ------------------------------------------------------------------------------


def _reply(content: str = "", *, calls: list[ToolCall] | None = None) -> ModelChatResult:
    return ModelChatResult(
        message=ChatMessage(role="assistant", content=content, tool_calls=calls or []),
        stop_reason="tool_use" if calls else "end_turn",
        usage=TokenUsage(),
    )


class _Script:
    """Plays `replies` in order; records every call's messages, and runs `during` on a call."""

    model_id = "wsn-fake"

    def __init__(self, *replies: ModelChatResult) -> None:
        self.replies = list(replies)
        self.calls: list[list[ChatMessage]] = []
        self.during: dict[int, Any] = {}

    async def chat(self, messages: list[ChatMessage], tools: list, opts: Any = None) -> ModelChatResult:
        self.calls.append(list(messages))
        hook = self.during.get(len(self.calls))
        if hook is not None:
            await hook()
        return self.replies[min(len(self.calls), len(self.replies)) - 1]

    async def stream_turn(self, messages: list[ChatMessage], tools: list, opts: Any = None) -> Any:
        result = await self.chat(messages, tools, opts)
        if result.message.content:
            yield StreamDelta(kind="text", text=result.message.content)
        yield result


class _Editor:
    """A tool whose first call coincides with the operator saving a file."""

    transport = "local"

    def __init__(self, thread: str) -> None:
        self.thread = thread
        self.calls = 0

    async def execute(self, args: ToolInput, ctx: ToolInvocationCtx | None = None) -> ToolOutput:
        self.calls += 1
        if self.calls == 1:
            queued = await workspace_notes.enqueue_if_running(
                TENANT, self.thread, WorkspaceNote(path="notes/plan.md", bytes=1204)
            )
            assert queued, "a run in flight was not marked active"
        return f"result {self.calls}"


def _agent(model: _Script, store: InMemorySessionStore, tool: _Editor | None = None) -> _ReactAgent:
    tools = [Tool(name="lookup", description="d", args_schema=None, executor=tool)] if tool else []
    agent = _ReactAgent(
        tools=tools,
        pattern="react",
        manifest_id="wsn",
        manifest_version="1",
        system_prompt="s",
        model_spec=ModelSpec(id="wsn-fake"),
        settings=None,
        recursion_limit=6,
        session_store=store,
        session_strategy=full_replay_session_strategy,
    )
    agent._resolve_model = lambda _input: model  # type: ignore[method-assign]
    return agent


def _ctx() -> RequestContext:
    settings = Settings(
        allow_insecure=True, auth_mode="none", environment="development", database_url="memory://wsn"
    )
    return RequestContext(settings=settings, auth=AuthContext(tenant_id=TENANT, scopes=frozenset()))


async def _stream(agent: _ReactAgent, thread: str, text: str) -> list[Any]:
    input = InvokeInput(messages=[ChatMessage(role="user", content=text)], tenant_id=TENANT, thread_id=thread)
    async with async_run_with_context(_ctx()):
        return [ev async for ev in agent.stream_events(input)]


def _note_count(messages: list[ChatMessage]) -> int:
    return sum(1 for m in messages if m.role == "user" and "notes/plan.md" in str(m.content))


async def test_a_note_sent_mid_batch_reaches_the_next_model_call_without_cancelling_the_batch() -> None:
    thread = "default:wsn-loop"
    store = InMemorySessionStore(tenant_id=TENANT)
    tool = _Editor(thread)
    calls = [ToolCall(id="c1", name="lookup", args={}), ToolCall(id="c2", name="lookup", args={})]
    model = _Script(_reply(calls=calls), _reply("done"))

    events = await _stream(_agent(model, store, tool), thread, "edit the plan")

    assert tool.calls == 2, "the note cancelled the rest of the batch"
    tool_ends = [ev.data for ev in events if ev.event == "tool_end"]
    assert all("cancelled" not in str(d) for d in tool_ends), tool_ends
    assert _note_count(model.calls[0]) == 0
    assert _note_count(model.calls[1]) == 1, "the next model call did not see the note"
    assert model.calls[1][-1].role == "user" and "1,204 bytes" in str(model.calls[1][-1].content)

    frames = [ev.data for ev in events if ev.event == "workspace_note"]
    assert frames == [{"path": "notes/plan.md", "op": "write", "bytes": 1204}]

    logged = [e for e in await store.open(thread).get_events() if e.kind == "custom"]
    assert len(logged) == 1
    (entry,) = logged
    assert entry.role == "user"
    md = entry.metadata or {}
    assert (md["type"], md["path"], md["op"], md["bytes"], md["source"], md["in_context"]) == (
        "workspace_edit",
        "notes/plan.md",
        "write",
        1204,
        "operator",
        True,
    )
    assert await workspace_notes.run_active(TENANT, thread) is False, "the run left itself marked"

    # The next run reads it once, from history — not a second time from a queue.
    second = _Script(_reply("again"))
    await _stream(_agent(second, store), thread, "and now?")
    assert _note_count(second.calls[0]) == 1, "the note was delivered twice"
    assert len([e for e in await store.open(thread).get_events() if e.kind == "custom"]) == 1


async def test_a_note_queued_after_the_last_model_call_is_logged_when_the_run_ends() -> None:
    """The run drains before each model call; one sent during the final call has no next
    call to reach, so the run writes it on its way out for the next run's history."""
    thread = "default:wsn-late"
    store = InMemorySessionStore(tenant_id=TENANT)
    model = _Script(_reply("only answer"))

    async def _late() -> None:
        assert await workspace_notes.enqueue_if_running(
            TENANT, thread, WorkspaceNote(path="late.md", op="delete")
        )

    model.during[1] = _late
    events = await _stream(_agent(model, store), thread, "hi")

    assert not [ev for ev in events if ev.event == "workspace_note"], "no live call read it"
    logged = [e for e in await store.open(thread).get_events() if e.kind == "custom"]
    assert [(e.metadata or {}).get("op") for e in logged] == ["delete"]
    assert await workspace_notes.drain(TENANT, thread) == []
