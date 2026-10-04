"""A turn takes its thread's leaf from the store once, before it appends anything.

The Postgres behaviour is in `tests/conformance/test_turn_leaf.py`; this pins the hook in
`_ReactAgent._run` without a database, through a store whose sessions keep their leaf durably
(they expose `resolve_leaf`) and log every call, so the main suite fails if the sync moves after
the first append or into the per-append path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from felix.patterns.model import ModelChatResult, TokenUsage
from felix.patterns.react import _ReactAgent
from felix.patterns.types import ChatMessage, InvokeInput
from felix.session import tree
from felix.session.types import AppendableEvent


@dataclass
class _Session:
    id: str
    log: list[str]
    stored_leaf: str
    events: list[AppendableEvent] = field(default_factory=list)

    async def resolve_leaf(self) -> str | None:
        self.log.append("resolve")
        return self.stored_leaf

    async def append_batch(self, events: list[AppendableEvent]) -> list[int]:
        self.log.append("append")
        self.events.extend(events)
        return list(range(len(events)))

    async def get_events(self, opts: Any = None) -> list[Any]:
        return []


class _Store:
    def __init__(self, stored_leaf: str) -> None:
        self.log: list[str] = []
        self.stored_leaf = stored_leaf
        self.appended: list[AppendableEvent] = []

    def open(self, thread_id: str) -> _Session:
        session = _Session(thread_id, self.log, self.stored_leaf)
        session.events = self.appended
        return session


class _Model:
    model_id = "claude-sonnet-4-5"

    async def chat(self, messages: list[ChatMessage], tools: list[Any], opts: Any = None) -> ModelChatResult:
        return ModelChatResult(
            message=ChatMessage(role="assistant", content="ok"),
            stop_reason="end_turn",
            usage=TokenUsage(input=1, output=1),
        )


@pytest.mark.asyncio
async def test_a_turn_syncs_the_leaf_once_before_its_first_append() -> None:
    thread = "default:leaf-sync"
    # This process's index holds a leaf another replica has since moved past.
    tree.set_leaf(thread, "stale")
    store = _Store(stored_leaf="stored")
    agent = _ReactAgent(
        tools=[],
        pattern="react",
        manifest_id="test",
        manifest_version="1",
        system_prompt="s",
        model_spec=None,
        settings=None,
        recursion_limit=3,
        session_store=store,
    )
    agent._resolve_model = lambda _i: _Model()  # type: ignore[method-assign]
    try:
        # A model change makes the turn append three times: the change, the user turn, the reply.
        await agent.invoke(
            InvokeInput(
                messages=[ChatMessage(role="user", content="hi")],
                thread_id=thread,
                model_id="claude-sonnet-4-5",
            )
        )
    finally:
        tree.set_leaf(thread, None)

    assert store.log[0] == "resolve"
    assert store.log.count("resolve") == 1, store.log
    assert store.log.count("append") >= 3, store.log
    first = store.appended[0]
    assert (first.metadata or {}).get("parent_id") == "stored"


@pytest.mark.asyncio
async def test_a_session_without_a_durable_leaf_keeps_the_index() -> None:
    """`memory://` and plugin checkpointers: the in-process index is the store, so it stands."""
    from felix.session.store import InMemorySessionStore

    thread = "default:leaf-sync-memory"
    tree.set_leaf(thread, "kept")
    try:
        assert await tree.sync_leaf(InMemorySessionStore(tenant_id="default").open(thread)) == "kept"
        assert tree.get_leaf(thread) == "kept"
    finally:
        tree.set_leaf(thread, None)


@pytest.mark.asyncio
async def test_a_read_of_the_stored_leaf_leaves_the_index_alone() -> None:
    """Export, a fork's source, the snapshot: they read the leaf and must not move it.

    Moving it outside the thread's lock could land inside a turn's append and set the
    index back to the row's older leaf.
    """
    thread = "default:leaf-read"
    tree.set_leaf(thread, "this-process")
    try:
        assert await tree.stored_leaf(_Session(thread, [], "stored")) == "stored"
        assert tree.get_leaf(thread) == "this-process"
    finally:
        tree.set_leaf(thread, None)
