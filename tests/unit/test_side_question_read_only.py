"""A side question renders through the manifest's strategy and writes nothing to the thread.

`POST /chat/ask` must leave the session log as it found it, and the strategy that renders the
thread is the manifest's own — a compacting one, over budget, summarises and *appends* the
summary as it renders. The e2e tests run on `full_replay`, which never writes, so they cannot
show that the read-only view holds; this drives the strategy that does write.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.patterns.model import ModelChatResult, TokenUsage
from felix.patterns.types import ChatMessage
from felix.session.compaction import CompactingSessionStrategy
from felix.session.side_question import _read_answer, _ReadOnlySession
from felix.session.store import InMemorySessionStore
from felix.session.tree import annotate_and_append
from felix.session.types import AppendableEvent


class _Summarizer:
    model_id = "fast"

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages: list[ChatMessage], tools: list[Any], opts: Any = None) -> ModelChatResult:
        self.calls += 1
        return ModelChatResult(
            message=ChatMessage(role="assistant", content="summary"),
            stop_reason="end_turn",
            usage=TokenUsage(),
        )


async def _long_session() -> Any:
    session = InMemorySessionStore(tenant_id="acme").open("acme:ask")
    for i in range(20):
        await annotate_and_append(
            session, [AppendableEvent(kind="message", role="user", content=("hello world " * 200) + str(i))]
        )
    return session


async def test_a_stored_summary_render_runs_no_pass_and_fires_no_hook() -> None:
    """What `POST /chat/ask` asks for: the cut a pass would make, without the pass."""
    from felix.hooks import get_agent_hooks, reset_agent_hooks

    reset_agent_hooks()
    fired: list[str] = []
    get_agent_hooks().register_before_compact(lambda prep, ctx: fired.append("before_compact"))
    get_agent_hooks().register_compact_failed(lambda info, ctx: fired.append("compact_failed"))
    try:
        session = await _long_session()
        before = await session.get_events()
        model = _Summarizer()
        readonly = _ReadOnlySession(session)

        rendered = await CompactingSessionStrategy(
            reserve_tokens=10, keep_recent_tokens=50, context_window_tokens=100
        ).render(
            readonly,
            [ChatMessage(role="user", content="q")],
            {"system_prompt": "sys", "model": model, "stored_summary_only": True},
        )
    finally:
        reset_agent_hooks()

    assert (model.calls, fired, readonly.dropped_writes) == (0, [], 0)
    assert await session.get_events() == before
    assert any("not shown" in (m.content or "") for m in rendered), "the frame says what it left out"
    assert len(rendered) < len(before), "and it is the cut window, not the whole thread"


async def test_a_compacting_render_over_budget_appends_nothing_through_the_read_only_view() -> None:
    session = await _long_session()
    before = await session.get_events()
    strategy = CompactingSessionStrategy(reserve_tokens=10, keep_recent_tokens=50, context_window_tokens=100)
    readonly = _ReadOnlySession(session)

    rendered = await strategy.render(
        readonly, [ChatMessage(role="user", content="q")], {"system_prompt": "sys", "model": _Summarizer()}
    )

    assert readonly.dropped_writes > 0, "the strategy did try to write: this is the case that matters"
    assert await session.get_events() == before
    assert rendered[-1].content == "q"


@pytest.mark.parametrize(
    ("reply", "status"),
    [
        ("NOT_IN_CONTEXT", "not_in_context"),
        ("  `NOT_IN_CONTEXT`.\n", "not_in_context"),
        ("The zucchini.", "answered"),
        ("It is NOT_IN_CONTEXT because the user never said.", "answered"),
    ],
)
def test_only_the_sentinel_alone_reads_not_in_context(reply: str, status: str) -> None:
    assert _read_answer(reply)[0] == status


async def test_the_summarizing_strategy_shows_its_stored_summary_without_a_new_one() -> None:
    from felix.session.strategies import SummarizingSessionStrategy

    session = await _long_session()
    model = _Summarizer()
    readonly = _ReadOnlySession(session)

    rendered = await SummarizingSessionStrategy(keep=3).render(
        readonly,
        [ChatMessage(role="user", content="q")],
        {"system_prompt": "sys", "model": model, "stored_summary_only": True},
    )

    assert (model.calls, readonly.dropped_writes) == (0, 0)
    assert any("stored summary only" in (m.content or "") for m in rendered)
