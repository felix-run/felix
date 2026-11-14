"""`windowed:N` and `semantic:N` keep a tool call and its results together.

Both select events one at a time -- the last N, the N most relevant -- and a provider rejects
a history in which a tool result has no call ahead of it, or a call has no result. A window
that opened on a tool result did exactly that.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.patterns.types import ChatMessage, ToolCall
from felix.session.store import InMemorySessionStore
from felix.session.strategies import SemanticSessionStrategy, WindowedSessionStrategy
from felix.session.tree import annotate_and_append
from felix.session.types import AppendableEvent, chat_message_to_event


def _call(call_id: str, name: str = "lookup") -> ToolCall:
    return ToolCall(id=call_id, name=name, args={})


# user / assistant(two calls) / tool / tool / assistant / user / assistant
THREAD = [
    ChatMessage(role="user", content="find the invoice"),
    ChatMessage(role="assistant", content="", tool_calls=[_call("c1"), _call("c2")]),
    ChatMessage(role="tool", tool_call_id="c1", name="lookup", content="nothing under that name"),
    ChatMessage(role="tool", tool_call_id="c2", name="lookup", content="invoice 4471 is overdue"),
    ChatMessage(role="assistant", content="It is 4471."),
    ChatMessage(role="user", content="thanks"),
    ChatMessage(role="assistant", content="Any time."),
]


async def _session(thread: str, messages: list[ChatMessage] = THREAD, pinned: frozenset[int] = frozenset()):
    session = InMemorySessionStore(tenant_id="default").open(thread)
    for i, message in enumerate(messages):
        event: AppendableEvent = chat_message_to_event(message)
        if i in pinned:
            event.metadata = {**(event.metadata or {}), "pinned": True}
        await annotate_and_append(session, [event])
    return session


async def _render(strategy: Any, session: Any, ask: str = "and the next one?") -> list[ChatMessage]:
    out = await strategy.render(session, [ChatMessage(role="user", content=ask)], {"system_prompt": "sys"})
    return [m for m in out if m.role != "system"]


def _assert_paired(messages: list[ChatMessage]) -> None:
    """Every tool result sits under the assistant that made its call, with only sibling results
    between them, and every call that reached the model has its result."""
    owner: ChatMessage | None = None
    for message in messages:
        if message.role == "tool":
            calls = [c.id for c in (owner.tool_calls or [])] if owner is not None else []
            assert message.tool_call_id in calls, (
                f"tool result {message.tool_call_id} has no call ahead of it"
            )
        else:
            owner = message if message.role == "assistant" and message.tool_calls else None
    called = [c.id for m in messages if m.role == "assistant" for c in (m.tool_calls or [])]
    answered = [m.tool_call_id for m in messages if m.role == "tool"]
    assert sorted(called) == sorted(answered), f"calls {called} against results {answered}"


@pytest.fixture
def keyword_ranking(monkeypatch: pytest.MonkeyPatch) -> None:
    """The keyword path, whether or not the embeddings extra is installed."""
    import felix.embeddings as embeddings

    async def _unavailable(*_a: Any, **_kw: Any) -> None:
        return None

    monkeypatch.setattr(embeddings, "rank_indices_by_query_async", _unavailable)


async def test_a_window_that_opens_on_a_tool_result_takes_its_call() -> None:
    """The last four events start on c2's result; c1's result and the call come with it."""
    seen = await _render(WindowedSessionStrategy(4), await _session("t:window-result"))
    assert [m.tool_call_id for m in seen if m.role == "tool"] == ["c1", "c2"]
    _assert_paired(seen)


async def test_a_window_that_opens_between_a_call_and_its_results_is_whole() -> None:
    seen = await _render(WindowedSessionStrategy(5), await _session("t:window-mid"))
    assert [m.tool_call_id for m in seen if m.role == "tool"] == ["c1", "c2"]
    _assert_paired(seen)


async def test_a_call_id_reused_in_a_later_turn_pairs_with_its_own_turn() -> None:
    """Some compatible servers number calls per response, so `call_0` is in every turn. Paired
    by id across the thread, the first turn's result came along with the second turn's call
    and led the history with no call ahead of it."""
    turn = [
        ChatMessage(role="assistant", content="", tool_calls=[_call("call_0")]),
        ChatMessage(role="tool", tool_call_id="call_0", name="lookup", content="found"),
        ChatMessage(role="assistant", content="Done."),
    ]
    thread = [
        ChatMessage(role="user", content="first"),
        *turn,
        ChatMessage(role="user", content="second"),
        *turn,
    ]
    seen = await _render(WindowedSessionStrategy(2), await _session("t:window-reused-id", thread))
    assert [m.role for m in seen[:-1]] == ["assistant", "tool", "assistant"]
    _assert_paired(seen)


async def test_a_window_clear_of_tool_calls_is_unchanged() -> None:
    seen = await _render(WindowedSessionStrategy(3), await _session("t:window-clear"))
    assert [m.content for m in seen[:-1]] == ["It is 4471.", "thanks", "Any time."]


async def test_a_pinned_tool_result_brings_its_call_into_the_window() -> None:
    seen = await _render(WindowedSessionStrategy(1), await _session("t:window-pin", pinned=frozenset({3})))
    assert [m.role for m in seen[:-1]] == ["assistant", "tool", "tool", "assistant"]
    _assert_paired(seen)


async def test_a_tool_result_whose_call_is_not_in_the_thread_is_dropped() -> None:
    orphaned = [ChatMessage(role="tool", tool_call_id="gone", name="lookup", content="stale"), *THREAD[4:]]
    seen = await _render(WindowedSessionStrategy(10), await _session("t:window-orphan", orphaned))
    assert [m.role for m in seen[:-1]] == ["assistant", "user", "assistant"]


async def test_a_semantic_pick_of_a_tool_result_takes_its_call(keyword_ranking: None) -> None:
    """The one event the query matches is c2's result."""
    session = await _session("t:semantic-result")
    seen = await _render(SemanticSessionStrategy(1), session, ask="invoice 4471 overdue")
    assert [m.tool_call_id for m in seen if m.role == "tool"] == ["c1", "c2"]
    _assert_paired(seen)


async def test_a_semantic_pick_of_a_call_takes_its_results(keyword_ranking: None) -> None:
    thread = [
        THREAD[0],
        ChatMessage(role="assistant", content="searching the ledger", tool_calls=[_call("c1")]),
    ]
    thread += [THREAD[2], *THREAD[4:]]
    seen = await _render(SemanticSessionStrategy(1), await _session("t:semantic-call", thread), ask="ledger")
    assert [m.role for m in seen[:-1]] == ["assistant", "tool"]
    _assert_paired(seen)
