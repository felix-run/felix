"""A turn's appends and its model context follow the leaf the store holds, not this process's.

New events parent on `tree._leaf_by_thread`, and the active branch -- what a session strategy
renders into the model's context -- is drawn from it. That index is per process. On Postgres a
replica that had not served the thread parented the next event at nothing, so it became a new
root and the model saw none of the history; one that had served it before another replica
rewound extended the branch the rewind abandoned.

`_other_replica()` empties every per-process index, which is the state a replica that has not
served the thread is in. Turns run through `_ReactAgent` with a real store and strategy, so
what is asserted is the production hook (`_ReactAgent._run` -> `tree.sync_leaf`), not a helper
called by hand.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import pytest
from felix.patterns.model import ModelChatResult, TokenUsage
from felix.patterns.types import ChatMessage, InvokeInput

TENANT = "conformance"

postgres_only = pytest.mark.parametrize("store_settings", ["postgres"], indirect=True)
both_arms = pytest.mark.parametrize("store_settings", ["memory", "postgres"], indirect=True)


def _thread() -> str:
    return f"{TENANT}:{uuid.uuid4().hex}"


def _other_replica() -> None:
    from felix.session import tree
    from felix.session.thread_state import reset_thread_meta_for_tests

    reset_thread_meta_for_tests()
    tree._leaf_by_thread.clear()
    tree._epoch_by_thread.clear()
    tree._label_by_event.clear()


class _Model:
    """Answers every call with ``reply`` and keeps what it was shown."""

    model_id = "claude-sonnet-4-5"

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.seen: list[list[ChatMessage]] = []

    async def chat(self, messages: list[ChatMessage], tools: list[Any], opts: Any = None) -> ModelChatResult:
        self.seen.append(list(messages))
        return ModelChatResult(
            message=ChatMessage(role="assistant", content=self.reply),
            stop_reason="end_turn",
            usage=TokenUsage(input=10, output=2),
        )


async def _turn(settings: Any, thread: str, text: str) -> _Model:
    from felix.patterns.react import _ReactAgent
    from felix.session.store import get_session_store
    from felix.session.strategies import FullReplaySessionStrategy

    model = _Model(f"re: {text}")
    agent = _ReactAgent(
        tools=[],
        pattern="react",
        manifest_id="conformance",
        manifest_version="1",
        system_prompt="s",
        model_spec=None,
        settings=settings,
        recursion_limit=3,
        session_store=get_session_store(settings, tenant_id=TENANT),
        session_strategy=FullReplaySessionStrategy(),
        tenant_id=TENANT,
    )
    agent._resolve_model = lambda _i: model  # type: ignore[method-assign]
    await agent.invoke(
        InvokeInput(messages=[ChatMessage(role="user", content=text)], thread_id=thread, tenant_id=TENANT)
    )
    return model


async def _events(settings: Any, thread: str) -> list[Any]:
    from felix.session.store import get_session_store

    return await get_session_store(settings, tenant_id=TENANT).open(thread).get_events()


def _by_content(events: list[Any], content: str) -> Any:
    found = [e for e in events if e.content == content]
    assert len(found) == 1, (content, [e.content for e in events])
    return found[0]


def _seen_text(model: _Model) -> list[str]:
    return [str(m.content) for m in model.seen[0] if m.role != "system"]


async def _row(settings: Any, thread: str) -> Any:
    from felix.db.models import ThreadState
    from felix.db.session import tenant_session

    async with tenant_session(settings, TENANT) as db:
        return await db.get(ThreadState, (TENANT, thread))


@postgres_only
async def test_a_cold_replica_continues_the_conversation(store_settings: Any) -> None:
    from felix.session.thread_state import update_thread_meta

    thread = _thread()
    await update_thread_meta(settings=store_settings, tenant_id=TENANT, thread_id=thread, session_name="n")
    await _turn(store_settings, thread, "one")
    _other_replica()

    model = await _turn(store_settings, thread, "two")

    events = await _events(store_settings, thread)
    earlier = _by_content(events, "re: one")
    assert _by_content(events, "two").metadata.get("parent_id") == earlier.metadata["event_id"]
    # The model's context is the branch: the first turn is in it, not a fresh root.
    assert _seen_text(model) == ["one", "re: one", "two"]


@postgres_only
async def test_a_warm_replica_takes_the_turn_after_another_replica_rewound(store_settings: Any) -> None:
    from felix.session import tree
    from felix.session.store import get_session_store
    from felix.session.thread_state import persist_leaf, update_thread_meta

    thread = _thread()
    await update_thread_meta(settings=store_settings, tenant_id=TENANT, thread_id=thread, session_name="n")
    await _turn(store_settings, thread, "one")
    await _turn(store_settings, thread, "two")
    target = _by_content(await _events(store_settings, thread), "re: one").metadata["event_id"]
    a_leaves = dict(tree._leaf_by_thread)

    # Replica B rewinds the thread to the first answer, the way `/chat/rewind` does.
    _other_replica()
    result = await tree.rewind_to(get_session_store(store_settings, tenant_id=TENANT).open(thread), target)
    await persist_leaf(
        settings=store_settings, tenant_id=TENANT, thread_id=thread, leaf_event_id=result["leaf_id"]
    )

    # Back on A, which still holds the leaf at the end of "two".
    tree._leaf_by_thread.clear()
    tree._leaf_by_thread.update(a_leaves)
    model = await _turn(store_settings, thread, "three")

    events = await _events(store_settings, thread)
    assert _by_content(events, "three").metadata.get("parent_id") == target
    assert _seen_text(model) == ["one", "re: one", "three"]


@postgres_only
async def test_a_legacy_row_yields_to_the_newest_event_and_is_rewritten(
    store_settings: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """A row from before the stored leaf followed appends holds an old rewind target.

    Here the conversation went on elsewhere after it -- a cold replica of that era started a
    new root -- and the row never heard. Its leaf is not the leaf, the newest event is.
    """
    from felix.session.thread_state import update_thread_meta

    thread = _thread()
    await update_thread_meta(settings=store_settings, tenant_id=TENANT, thread_id=thread, session_name="n")
    await _turn(store_settings, thread, "one")
    old_target = _by_content(await _events(store_settings, thread), "one").metadata["event_id"]
    _other_replica()
    await _turn(store_settings, thread, "elsewhere")
    newest = _by_content(await _events(store_settings, thread), "re: elsewhere").metadata["event_id"]
    await _make_legacy(store_settings, thread, leaf=old_target)
    _other_replica()

    # Alembic's `fileConfig` disables every logger that exists when a migration test runs
    # before this one, so the logger is re-enabled for the turn rather than trusted.
    store_logger = logging.getLogger("felix.session.store")
    was_disabled, store_logger.disabled = store_logger.disabled, False
    try:
        with caplog.at_level(logging.WARNING, logger="felix.session.store"):
            await _turn(store_settings, thread, "next")
    finally:
        store_logger.disabled = was_disabled

    events = await _events(store_settings, thread)
    assert _by_content(events, "next").metadata.get("parent_id") == newest
    assert any(
        thread in r.getMessage() and "predates leaf tracking" in r.getMessage() for r in caplog.records
    )
    from felix.session.thread_state import LEAF_TRACKED_KEY, get_thread_meta, leaf_is_tracked

    row = await _row(store_settings, thread)
    assert leaf_is_tracked(row.labels_json), row.labels_json
    assert row.leaf_event_id == _by_content(events, "re: next").metadata["event_id"]
    # The mark is bookkeeping, not metadata a caller reads back.
    meta = await get_thread_meta(settings=store_settings, tenant_id=TENANT, thread_id=thread)
    assert LEAF_TRACKED_KEY not in meta


@postgres_only
async def test_resolving_a_legacy_row_without_appending_still_rewrites_it(store_settings: Any) -> None:
    """A fork or an export resolves the leaf and appends nothing to the thread.

    The rewrite is what moves the row then -- so `load_leaf`, which the session snapshot
    reads, stops answering the stale leaf, and the next turn is one primary-key read.
    """
    from felix.session import tree
    from felix.session.store import get_session_store
    from felix.session.thread_state import load_leaf, update_thread_meta

    thread = _thread()
    await update_thread_meta(settings=store_settings, tenant_id=TENANT, thread_id=thread, session_name="n")
    await _turn(store_settings, thread, "one")
    await _turn(store_settings, thread, "two")
    events = await _events(store_settings, thread)
    await _make_legacy(store_settings, thread, leaf=_by_content(events, "one").metadata["event_id"])
    _other_replica()

    store = get_session_store(store_settings, tenant_id=TENANT)
    await tree.fork_thread(store.open(thread), store.open(_thread()))

    newest = _by_content(events, "re: two").metadata["event_id"]
    assert await load_leaf(settings=store_settings, tenant_id=TENANT, thread_id=thread) == newest
    from felix.session.thread_state import leaf_is_tracked

    assert leaf_is_tracked((await _row(store_settings, thread)).labels_json)


@postgres_only
async def test_a_legacy_row_behind_its_own_branch_yields_to_the_newest_event(store_settings: Any) -> None:
    """The legacy row's turn *after* its rewind is on the rewind's branch, and still not in the row.

    The leaf is the newest event, not the stored ancestor: taking the ancestor would make the
    next event a sibling of the turn the user last saw, and drop it from the context.
    """
    from felix.session.thread_state import update_thread_meta

    thread = _thread()
    await update_thread_meta(settings=store_settings, tenant_id=TENANT, thread_id=thread, session_name="n")
    await _turn(store_settings, thread, "one")
    ancestor = _by_content(await _events(store_settings, thread), "re: one").metadata["event_id"]
    await _turn(store_settings, thread, "two")
    newest = _by_content(await _events(store_settings, thread), "re: two").metadata["event_id"]
    await _make_legacy(store_settings, thread, leaf=ancestor)
    _other_replica()

    model = await _turn(store_settings, thread, "three")

    assert _by_content(await _events(store_settings, thread), "three").metadata.get("parent_id") == newest
    assert _seen_text(model) == ["one", "re: one", "two", "re: two", "three"]


@postgres_only
async def test_a_rewind_survives_a_cold_replica(store_settings: Any) -> None:
    """After a rewind the newest event is on the abandoned branch; the rewind still wins."""
    from felix.session import tree
    from felix.session.store import get_session_store
    from felix.session.thread_state import persist_leaf, update_thread_meta

    thread = _thread()
    await update_thread_meta(settings=store_settings, tenant_id=TENANT, thread_id=thread, session_name="n")
    await _turn(store_settings, thread, "one")
    await _turn(store_settings, thread, "two")
    target = _by_content(await _events(store_settings, thread), "re: one").metadata["event_id"]
    result = await tree.rewind_to(get_session_store(store_settings, tenant_id=TENANT).open(thread), target)
    await persist_leaf(
        settings=store_settings, tenant_id=TENANT, thread_id=thread, leaf_event_id=result["leaf_id"]
    )
    _other_replica()

    model = await _turn(store_settings, thread, "three")

    assert _by_content(await _events(store_settings, thread), "three").metadata.get("parent_id") == target
    assert _seen_text(model) == ["one", "re: one", "three"]


@postgres_only
async def test_a_rewind_of_a_legacy_row_is_honoured(store_settings: Any) -> None:
    """The first leaf write a legacy row gets can be a rewind, before any turn has marked it."""
    from felix.session import tree
    from felix.session.store import get_session_store
    from felix.session.thread_state import persist_leaf, update_thread_meta

    thread = _thread()
    await update_thread_meta(settings=store_settings, tenant_id=TENANT, thread_id=thread, session_name="n")
    await _turn(store_settings, thread, "one")
    await _turn(store_settings, thread, "two")
    newest = _by_content(await _events(store_settings, thread), "re: two").metadata["event_id"]
    await _make_legacy(store_settings, thread, leaf=newest)
    _other_replica()
    target = _by_content(await _events(store_settings, thread), "re: one").metadata["event_id"]
    result = await tree.rewind_to(get_session_store(store_settings, tenant_id=TENANT).open(thread), target)
    await persist_leaf(
        settings=store_settings, tenant_id=TENANT, thread_id=thread, leaf_event_id=result["leaf_id"]
    )
    _other_replica()

    await _turn(store_settings, thread, "three")

    assert _by_content(await _events(store_settings, thread), "three").metadata.get("parent_id") == target


@postgres_only
async def test_a_fork_from_a_cold_replica_copies_the_stored_branch(store_settings: Any) -> None:
    from felix.session import tree
    from felix.session.store import get_session_store
    from felix.session.thread_state import persist_leaf, update_thread_meta

    thread = _thread()
    await update_thread_meta(settings=store_settings, tenant_id=TENANT, thread_id=thread, session_name="n")
    await _turn(store_settings, thread, "one")
    await _turn(store_settings, thread, "two")
    store = get_session_store(store_settings, tenant_id=TENANT)
    target = _by_content(await _events(store_settings, thread), "re: one").metadata["event_id"]
    result = await tree.rewind_to(store.open(thread), target)
    await persist_leaf(
        settings=store_settings, tenant_id=TENANT, thread_id=thread, leaf_event_id=result["leaf_id"]
    )
    _other_replica()

    dest = _thread()
    forked = await tree.fork_thread(store.open(thread), store.open(dest))

    assert forked["copied"] == 2
    assert [e.content for e in await _events(store_settings, dest)] == ["one", "re: one"]


@both_arms
async def test_an_unknown_thread_has_no_leaf_and_is_not_created(store_settings: Any) -> None:
    from felix.session.store import get_session_store
    from felix.session.tree import sync_leaf

    from tests.support.session_listing import every_listed

    thread = _thread()
    assert await sync_leaf(get_session_store(store_settings, tenant_id=TENANT).open(thread)) is None
    listed = {str(m["id"]) for m in await every_listed(store_settings, TENANT)}
    assert thread not in listed


@postgres_only
async def test_the_leaf_is_resolved_once_per_turn(
    store_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once per turn, whatever the turn appends -- and one primary-key read on a tracked row."""
    from felix.session.store import _PostgresSession
    from felix.session.thread_state import update_thread_meta
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    thread = _thread()
    await update_thread_meta(settings=store_settings, tenant_id=TENANT, thread_id=thread, session_name="n")
    await _turn(store_settings, thread, "one")
    _other_replica()

    calls: list[str] = []
    statements: list[str] = []
    real = _PostgresSession.resolve_leaf

    async def counted(self: _PostgresSession) -> str | None:
        calls.append(self.id)
        statements.clear()
        try:
            return await real(self)
        finally:
            # Table reads only: a transaction's `set_config` for RLS is not a query of the store.
            tables = [t for t in ("thread_state", "session_events") for q in statements if t in q]
            calls.append(f"read {', '.join(tables)}")

    def record(_conn: Any, _cursor: Any, statement: str, *_a: Any) -> None:
        statements.append(statement)

    monkeypatch.setattr(_PostgresSession, "resolve_leaf", counted)
    event.listen(Engine, "before_cursor_execute", record)
    try:
        # A turn with a model change appends three times: the change, the user turn, the reply.
        from felix.patterns.react import _ReactAgent

        original = _ReactAgent.invoke

        async def with_model_change(self: Any, input: InvokeInput) -> Any:
            input.model_id = "claude-sonnet-4-5"
            return await original(self, input)

        monkeypatch.setattr(_ReactAgent, "invoke", with_model_change)
        await _turn(store_settings, thread, "two")
    finally:
        event.remove(Engine, "before_cursor_execute", record)

    assert calls == [thread, "read thread_state"], calls
    kinds = [e.kind for e in await _events(store_settings, thread)]
    assert kinds.count("model_change") == 1


async def _make_legacy(settings: Any, thread: str, *, leaf: str) -> None:
    """Rewrite a row as code before the stored leaf followed appends left it: no mark, old leaf."""
    from felix.db.models import ThreadState
    from felix.db.session import tenant_session
    from felix.session.thread_state import _row_meta

    async with tenant_session(settings, TENANT) as db:
        row = await db.get(ThreadState, (TENANT, thread))
        assert row is not None
        # The metadata a read answers, which carries none of the leaf's bookkeeping.
        row.labels_json = _row_meta(row.labels_json)
        row.leaf_event_id = leaf
        await db.commit()


# --- races inside one replica, and between two ------------------------------------------------


async def _turn_with_interleave(
    settings: Any,
    thread: str,
    text: str,
    monkeypatch: pytest.MonkeyPatch,
    inject: Any,
    *,
    at_reply: bool = False,
) -> None:
    """Run a turn, starting ``inject`` between an append's `set_leaf` and its `store_leaf`.

    The first append's, or with ``at_reply`` the one that writes the model's answer -- the
    turn's last, so nothing the turn does afterwards re-converges the row and the index. The
    injection runs as its own task, the way a concurrent request on this replica would. It
    is given half a second to finish before the append's row write goes ahead: enough for a
    read that nothing blocks, and a timeout -- not a deadlock -- for one the thread's lock holds.
    """
    import asyncio

    from felix.session.store import _PostgresSession

    real = _PostgresSession.store_leaf
    pending: list[asyncio.Task[Any]] = []

    async def is_reply(session: _PostgresSession, event_id: str) -> bool:
        appended = [e for e in await session.get_events() if (e.metadata or {}).get("event_id") == event_id]
        return bool(appended) and appended[0].role == "assistant"

    async def interleaved(self: _PostgresSession, event_id: str) -> None:
        if not pending and self.id == thread and (not at_reply or await is_reply(self, event_id)):
            pending.append(asyncio.create_task(inject()))
            await asyncio.wait(pending, timeout=0.5)
        await real(self, event_id)

    monkeypatch.setattr(_PostgresSession, "store_leaf", interleaved)
    await _turn(settings, thread, text)
    monkeypatch.setattr(_PostgresSession, "store_leaf", real)
    assert pending, "the turn never stored a leaf"
    await pending[0]


@postgres_only
async def test_a_read_route_inside_a_turns_append_does_not_move_its_leaf(
    store_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An export, a fork or a snapshot on this replica while a turn appends: the turn stays whole."""
    from felix.session import tree
    from felix.session.store import get_session_store

    thread = _thread()
    await _seed(store_settings, thread)
    store = get_session_store(store_settings, tenant_id=TENANT)

    await _turn_with_interleave(
        store_settings, thread, "two", monkeypatch, lambda: tree.stored_leaf(store.open(thread))
    )

    events = await _events(store_settings, thread)
    user = _by_content(events, "two").metadata["event_id"]
    assert _by_content(events, "re: two").metadata.get("parent_id") == user


@postgres_only
async def test_a_sync_inside_a_turns_append_waits_for_it(
    store_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second sync on this replica -- a compaction, another turn starting -- waits on the lock."""
    from felix.session import tree
    from felix.session.store import get_session_store

    thread = _thread()
    await _seed(store_settings, thread)
    store = get_session_store(store_settings, tenant_id=TENANT)

    await _turn_with_interleave(
        store_settings, thread, "two", monkeypatch, lambda: tree.sync_leaf(store.open(thread))
    )

    events = await _events(store_settings, thread)
    user = _by_content(events, "two").metadata["event_id"]
    assert _by_content(events, "re: two").metadata.get("parent_id") == user


@postgres_only
async def test_a_rewind_landing_during_a_legacy_fallback_wins(
    store_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replica Y rewinds while replica X is between reading an unmarked row and adopting a leaf."""
    from felix.session import tree
    from felix.session.store import _PostgresSession, get_session_store
    from felix.session.thread_state import load_leaf, persist_leaf

    thread = _thread()
    await _seed(store_settings, thread)
    await _turn(store_settings, thread, "two")
    events = await _events(store_settings, thread)
    target = _by_content(events, "re: one").metadata["event_id"]
    await _make_legacy(store_settings, thread, leaf=_by_content(events, "one").metadata["event_id"])
    _other_replica()

    real = _PostgresSession.adopt_leaf

    async def rewound_first(self: _PostgresSession, event_id: str) -> bool:
        await persist_leaf(settings=store_settings, tenant_id=TENANT, thread_id=thread, leaf_event_id=target)
        return await real(self, event_id)

    monkeypatch.setattr(_PostgresSession, "adopt_leaf", rewound_first)
    resolved = await tree.stored_leaf(get_session_store(store_settings, tenant_id=TENANT).open(thread))

    assert await load_leaf(settings=store_settings, tenant_id=TENANT, thread_id=thread) == target
    assert resolved == target


@postgres_only
async def test_the_snapshot_shows_a_legacy_threads_real_leaf(store_settings: Any) -> None:
    from felix.session import tree
    from felix.session.snapshot import gather_thread_snapshot

    thread = _thread()
    await _seed(store_settings, thread)
    await _turn(store_settings, thread, "two")
    events = await _events(store_settings, thread)
    await _make_legacy(store_settings, thread, leaf=_by_content(events, "one").metadata["event_id"])
    _other_replica()

    snapshot = await gather_thread_snapshot(settings=store_settings, tenant_id=TENANT, thread=thread)

    assert snapshot["leafId"] == _by_content(events, "re: two").metadata["event_id"]
    assert tree.get_leaf(thread) is None, "a snapshot reads the leaf; it does not set it"


async def _seed(settings: Any, thread: str) -> None:
    """A thread with a session row and one turn ("one" / "re: one")."""
    from felix.session.thread_state import update_thread_meta

    await update_thread_meta(settings=settings, tenant_id=TENANT, thread_id=thread, session_name="n")
    await _turn(settings, thread, "one")


@postgres_only
async def test_a_route_append_on_a_cold_replica_parents_on_the_stored_leaf(store_settings: Any) -> None:
    """`/chat/sessions/label`, `/name`, `/custom`, `/thinking` append outside any turn."""
    from felix.session.store import get_session_store
    from felix.session.tree import annotate_and_append
    from felix.session.types import AppendableEvent

    thread = _thread()
    await _seed(store_settings, thread)
    _other_replica()

    [label] = await annotate_and_append(
        get_session_store(store_settings, tenant_id=TENANT).open(thread),
        [AppendableEvent(kind="label", content="checkpoint", metadata={"type": "label"})],
        sync=True,
    )

    events = await _events(store_settings, thread)
    appended = next(e for e in events if e.metadata.get("event_id") == label)
    assert appended.metadata.get("parent_id") == _by_content(events, "re: one").metadata["event_id"]


# --- a rewind or a fork on the same replica as a turn -----------------------------------------
#
# Each is a sequence -- read the old leaf, move this process's leaf, maybe append a summary,
# store the leaf -- and a turn's append on this replica could land inside it. Under the
# thread's lock either may go first; what must hold is that the row and this process's leaf
# agree afterwards and that the rewind is not silently undone.


async def _assert_row_and_index_agree(settings: Any, thread: str) -> str | None:
    from felix.session import tree
    from felix.session.thread_state import load_leaf

    stored = await load_leaf(settings=settings, tenant_id=TENANT, thread_id=thread)
    assert stored == tree.get_leaf(thread), "the row and this process's leaf disagree"
    return stored


def _branch_ids(events: list[Any], leaf: str | None) -> list[str]:
    from felix.session import tree

    return [e.metadata["event_id"] for e in tree.active_branch_events(events, leaf_id=leaf)]


def _rewind(settings: Any, thread: str, target: str, *, summarize: bool = False) -> Any:
    """`/chat/rewind`'s sequence, as the route calls it."""
    from felix.session.branch import rewind_and_persist
    from felix.session.store import get_session_store

    session = get_session_store(settings, tenant_id=TENANT).open(thread)
    return rewind_and_persist(session, target, settings=settings, tenant_id=TENANT, summarize=summarize)


@postgres_only
async def test_a_rewind_inside_a_turns_append_is_not_overwritten_by_it(
    store_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rewind starts between the reply's `set_leaf` and its `store_leaf`.

    Unlocked, the rewind stored its target and then the reply's `store_leaf` wrote the reply
    over it: the row named the abandoned branch while this process's leaf named the target.
    """
    thread = _thread()
    await _seed(store_settings, thread)
    target = _by_content(await _events(store_settings, thread), "re: one").metadata["event_id"]

    await _turn_with_interleave(
        store_settings,
        thread,
        "two",
        monkeypatch,
        lambda: _rewind(store_settings, thread, target),
        at_reply=True,
    )

    leaf = await _assert_row_and_index_agree(store_settings, thread)
    events = await _events(store_settings, thread)
    on_branch = _branch_ids(events, leaf)
    assert target in on_branch
    assert _by_content(events, "two").metadata["event_id"] not in on_branch, "the rewind was undone"


@both_arms
async def test_a_turn_starting_inside_a_rewind_waits_for_it(
    store_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A turn arrives after the rewind has moved this process's leaf and before it stored it."""
    import asyncio

    from felix.session import thread_state

    thread = _thread()
    await _seed(store_settings, thread)
    await _turn(store_settings, thread, "two")
    target = _by_content(await _events(store_settings, thread), "re: one").metadata["event_id"]

    real = thread_state.persist_leaf
    turn: list[asyncio.Task[Any]] = []
    finished_inside: list[bool] = []

    async def paused(**kwargs: Any) -> None:
        if not turn:
            turn.append(asyncio.create_task(_turn(store_settings, thread, "three")))
            done, _ = await asyncio.wait(turn, timeout=0.5)
            finished_inside.append(bool(done))
        await real(**kwargs)

    monkeypatch.setattr(thread_state, "persist_leaf", paused)
    await _rewind(store_settings, thread, target)
    monkeypatch.setattr(thread_state, "persist_leaf", real)
    await turn[0]

    assert finished_inside == [False], "the turn ran inside the rewind instead of waiting for it"
    leaf = await _assert_row_and_index_agree(store_settings, thread)
    events = await _events(store_settings, thread)
    assert _by_content(events, "three").metadata.get("parent_id") == target
    assert leaf == _by_content(events, "re: three").metadata["event_id"]


@both_arms
async def test_a_summarising_rewind_completes_and_its_summary_is_on_the_new_branch(
    store_settings: Any,
) -> None:
    """The summary is appended inside the rewind's hold of a lock that is not reentrant."""
    import asyncio

    thread = _thread()
    await _seed(store_settings, thread)
    await _turn(store_settings, thread, "two")
    target = _by_content(await _events(store_settings, thread), "re: one").metadata["event_id"]

    result = await asyncio.wait_for(_rewind(store_settings, thread, target, summarize=True), timeout=5)

    summary_id = result["branch_summary"]["event_id"]
    events = await _events(store_settings, thread)
    [summary] = [e for e in events if e.metadata.get("event_id") == summary_id]
    assert summary.kind == "branch_summary"
    assert summary.metadata.get("parent_id") == target
    assert await _assert_row_and_index_agree(store_settings, thread) == summary_id


@postgres_only
async def test_a_fork_into_a_live_thread_inside_its_turns_append_is_refused(
    store_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`/chat/fork` names its destination, which may be a thread with a turn in flight.

    It is refused, and the turn's reply stays the leaf: nothing was copied in under it.
    """
    from felix.session.branch import fork_and_persist
    from felix.session.store import get_session_store

    source, dest = _thread(), _thread()
    await _seed(store_settings, source)
    await _seed(store_settings, dest)
    store = get_session_store(store_settings, tenant_id=TENANT)
    results: list[Any] = []

    async def fork() -> None:
        results.append(
            await fork_and_persist(
                store.open(source), store.open(dest), settings=store_settings, tenant_id=TENANT
            )
        )

    await _turn_with_interleave(store_settings, dest, "two", monkeypatch, fork, at_reply=True)

    assert results == [{"ok": False, "error": "thread_exists", "thread_id": dest}]
    leaf = await _assert_row_and_index_agree(store_settings, dest)
    events = await _events(store_settings, dest)
    assert leaf == _by_content(events, "re: two").metadata["event_id"]
    assert not [e for e in events if e.metadata.get("forked_from")], "the fork copied into a live thread"


# --- two forks to one new id ----------------------------------------------------------------
#
# Each pauses after its existence check has answered "no", so the other's check runs before the
# first has written anything. On one replica the second check waits on the destination's lock and
# finds the first fork's thread; across replicas there is no shared lock, and the metadata claim
# is what refuses the second. Both cases must leave one fork standing and one copy in the log.


async def _two_forks_to_one_id(settings: Any, monkeypatch: pytest.MonkeyPatch) -> tuple[list[Any], str]:
    import asyncio

    from felix.session import thread_state
    from felix.session.branch import fork_and_persist
    from felix.session.store import get_session_store

    source, dest = _thread(), _thread()
    await _seed(settings, source)
    store = get_session_store(settings, tenant_id=TENANT)
    real = thread_state.thread_exists

    async def checked_then_paused(*args: Any, **kwargs: Any) -> bool:
        answer = await real(*args, **kwargs)
        await asyncio.sleep(0.05)
        return answer

    monkeypatch.setattr(thread_state, "thread_exists", checked_then_paused)
    results = await asyncio.gather(
        *(
            fork_and_persist(store.open(source), store.open(dest), settings=settings, tenant_id=TENANT)
            for _ in range(2)
        )
    )
    monkeypatch.setattr(thread_state, "thread_exists", real)
    return list(results), dest


async def _assert_one_fork_landed(settings: Any, results: list[Any], dest: str) -> None:
    assert sorted(bool(r.get("ok")) for r in results) == [False, True], results
    [refused] = [r for r in results if not r.get("ok")]
    assert refused["error"] == "thread_exists"
    copied = [e for e in await _events(settings, dest) if e.metadata.get("forked_from")]
    assert len(copied) == 2, [e.content for e in copied]


@both_arms
async def test_two_forks_to_one_new_id_on_one_replica_leave_one_fork(
    store_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    results, dest = await _two_forks_to_one_id(store_settings, monkeypatch)
    await _assert_one_fork_landed(store_settings, results, dest)


@both_arms
async def test_two_forks_to_one_new_id_on_two_replicas_leave_one_fork(
    store_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two replicas share no `leaf_lock`, so it is replaced with one that serialises nothing."""
    from contextlib import asynccontextmanager

    from felix.session import branch

    @asynccontextmanager
    async def per_replica_lock(_session: Any) -> Any:
        yield

    monkeypatch.setattr(branch, "leaf_lock", per_replica_lock)
    results, dest = await _two_forks_to_one_id(store_settings, monkeypatch)
    await _assert_one_fork_landed(store_settings, results, dest)


# --- a rewind on another replica while a turn appends here -------------------------------------
#
# No in-process lock spans replicas, so each test plays replica Y inside one of replica X's
# `store_leaf` calls: X's leaf index and epoch are set aside, Y writes through the same functions
# a route would, and X's are put back before X's own write goes ahead. What stands between Y's
# rewind and X's append is the row's `leaf_epoch`.


async def _turn_with_other_replica(
    settings: Any, thread: str, text: str, monkeypatch: pytest.MonkeyPatch, other: Any
) -> None:
    """Run a turn on X, running ``other`` as replica Y inside X's first `store_leaf` for ``thread``."""
    from felix.session import tree
    from felix.session.store import _PostgresSession

    real = _PostgresSession.store_leaf
    ran: list[bool] = []

    async def interleaved(self: _PostgresSession, event_id: str) -> None:
        if not ran and self.id == thread:
            ran.append(True)
            x_state = (dict(tree._leaf_by_thread), dict(tree._epoch_by_thread))
            _other_replica()
            await other()
            tree._leaf_by_thread.clear()
            tree._leaf_by_thread.update(x_state[0])
            tree._epoch_by_thread.clear()
            tree._epoch_by_thread.update(x_state[1])
        await real(self, event_id)

    monkeypatch.setattr(_PostgresSession, "store_leaf", interleaved)
    await _turn(settings, thread, text)
    monkeypatch.setattr(_PostgresSession, "store_leaf", real)
    assert ran, "the turn never stored a leaf"


@postgres_only
async def test_a_rewind_on_another_replica_mid_turn_holds(
    store_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Y rewinds between X's turn resolving the leaf and X storing it; no later append undoes it.

    Unconditional, X's user event and then its reply were each written over the rewind, and the
    next turn -- on any replica -- continued the branch the user had just left.
    """
    from felix.session.thread_state import LEAF_EPOCH_KEY, persist_leaf

    thread = _thread()
    await _seed(store_settings, thread)
    await _turn(store_settings, thread, "two")
    target = _by_content(await _events(store_settings, thread), "re: one").metadata["event_id"]

    async def rewind_on_y() -> None:
        await persist_leaf(settings=store_settings, tenant_id=TENANT, thread_id=thread, leaf_event_id=target)

    await _turn_with_other_replica(store_settings, thread, "three", monkeypatch, rewind_on_y)

    row = await _row(store_settings, thread)
    assert row.leaf_event_id == target, "a turn's append overwrote another replica's rewind"
    assert row.labels_json[LEAF_EPOCH_KEY] == 1
    # The turn's events are in the log, on the abandoned branch, and the next turn follows Y.
    events = await _events(store_settings, thread)
    assert (
        _by_content(events, "re: three").metadata.get("parent_id")
        == _by_content(events, "three").metadata["event_id"]
    )
    model = await _turn(store_settings, thread, "four")
    assert _by_content(await _events(store_settings, thread), "four").metadata.get("parent_id") == target
    assert _seen_text(model) == ["one", "re: one", "four"]


@postgres_only
async def test_two_turns_on_two_replicas_without_a_rewind_stay_last_writer_wins(
    store_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An append bumps no epoch, so Y's append mid-turn does not stop X's from storing after it."""
    from felix.session import tree
    from felix.session.store import get_session_store
    from felix.session.types import AppendableEvent

    thread = _thread()
    await _seed(store_settings, thread)

    async def append_on_y() -> None:
        # Y's own lock is not X's, so Y's sync'd append runs the locked body directly.
        await tree._append_under_lock(
            get_session_store(store_settings, tenant_id=TENANT).open(thread),
            thread,
            [AppendableEvent(kind="custom", content="from y", metadata={"type": "custom"})],
            sync=True,
        )

    await _turn_with_other_replica(store_settings, thread, "two", monkeypatch, append_on_y)

    events = await _events(store_settings, thread)
    assert (await _row(store_settings, thread)).leaf_event_id == _by_content(events, "re: two").metadata[
        "event_id"
    ]


@postgres_only
async def test_a_turn_on_a_rewound_thread_stores_its_leaf(store_settings: Any) -> None:
    """The epoch a rewind left is the one the next turn resolves, and its appends store against it."""
    from felix.session.thread_state import LEAF_EPOCH_KEY, get_thread_meta

    thread = _thread()
    await _seed(store_settings, thread)
    await _turn(store_settings, thread, "two")
    target = _by_content(await _events(store_settings, thread), "re: one").metadata["event_id"]
    await _rewind(store_settings, thread, target)
    _other_replica()

    await _turn(store_settings, thread, "three")

    row = await _row(store_settings, thread)
    assert (
        row.leaf_event_id
        == _by_content(await _events(store_settings, thread), "re: three").metadata["event_id"]
    )
    assert row.labels_json[LEAF_EPOCH_KEY] == 1, "an append moved the epoch"
    meta = await get_thread_meta(settings=store_settings, tenant_id=TENANT, thread_id=thread)
    assert LEAF_EPOCH_KEY not in meta


@postgres_only
async def test_a_failed_leaf_write_mid_turn_does_not_block_the_turns_later_ones(
    store_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rejected compare-and-set's failure mode: one transient error stranding the whole turn."""
    import dataclasses

    from felix.session.store import _PostgresSession

    thread = _thread()
    await _seed(store_settings, thread)
    await _turn(store_settings, thread, "two")
    await _rewind(
        store_settings,
        thread,
        _by_content(await _events(store_settings, thread), "re: one").metadata["event_id"],
    )
    _other_replica()

    real = _PostgresSession.store_leaf
    failed: list[str] = []

    def unreachable() -> Any:
        raise ConnectionError("database went away")

    async def flaky(self: _PostgresSession, event_id: str) -> None:
        if not failed and self.id == thread:
            failed.append(event_id)
            await real(dataclasses.replace(self, session_factory=unreachable), event_id)
            return
        await real(self, event_id)

    monkeypatch.setattr(_PostgresSession, "store_leaf", flaky)
    await _turn(store_settings, thread, "three")
    monkeypatch.setattr(_PostgresSession, "store_leaf", real)

    events = await _events(store_settings, thread)
    assert failed == [_by_content(events, "three").metadata["event_id"]]
    assert (await _row(store_settings, thread)).leaf_event_id == _by_content(events, "re: three").metadata[
        "event_id"
    ]


@postgres_only
async def test_a_tracked_row_from_before_the_epoch_reads_as_epoch_zero(store_settings: Any) -> None:
    """A row #468 wrote has `leaf_v` and no `leaf_epoch`: appends store, and a rewind starts it at 1."""
    from felix.db.models import ThreadState
    from felix.db.session import tenant_session
    from felix.session.thread_state import LEAF_EPOCH_KEY, persist_leaf

    thread = _thread()
    await _seed(store_settings, thread)
    async with tenant_session(store_settings, TENANT) as db:
        row = await db.get(ThreadState, (TENANT, thread))
        assert row is not None
        row.labels_json = {k: v for k, v in row.labels_json.items() if k != LEAF_EPOCH_KEY}
        await db.commit()
    _other_replica()

    await _turn(store_settings, thread, "two")
    events = await _events(store_settings, thread)
    row = await _row(store_settings, thread)
    assert row.leaf_event_id == _by_content(events, "re: two").metadata["event_id"]
    assert LEAF_EPOCH_KEY not in row.labels_json

    target = _by_content(events, "re: one").metadata["event_id"]
    await persist_leaf(settings=store_settings, tenant_id=TENANT, thread_id=thread, leaf_event_id=target)
    assert (await _row(store_settings, thread)).labels_json[LEAF_EPOCH_KEY] == 1


@postgres_only
async def test_a_fork_starts_its_new_thread_at_epoch_one(store_settings: Any) -> None:
    from felix.session.branch import fork_and_persist
    from felix.session.store import get_session_store
    from felix.session.thread_state import LEAF_EPOCH_KEY

    source, dest = _thread(), _thread()
    await _seed(store_settings, source)
    store = get_session_store(store_settings, tenant_id=TENANT)

    result = await fork_and_persist(
        store.open(source), store.open(dest), settings=store_settings, tenant_id=TENANT
    )

    row = await _row(store_settings, dest)
    assert row.leaf_event_id == result["leaf_id"]
    assert row.labels_json[LEAF_EPOCH_KEY] == 1
    _other_replica()
    await _turn(store_settings, dest, "two")
    assert (await _row(store_settings, dest)).leaf_event_id == _by_content(
        await _events(store_settings, dest), "re: two"
    ).metadata["event_id"]
