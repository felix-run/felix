"""The session summarizer is a model call on the tenant's behalf and is metered as one.

Compaction wrote its own usage row against the literal tenant `"default"`, priced it by
the logical route name (which the price table does not know), and never touched the
run's `limit_state` — so on a long thread the single largest input-token call escaped
`limits.max_cost_usd` and landed on another tenant's bill. The `summarizing:N` strategy
recorded nothing at all. Both now go through `record_model_usage`, like the turn itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from felix.config import Settings
from felix.context import AuthContext, RequestContext, async_run_with_context
from felix.patterns.model import ModelChatResult, TokenUsage, record_model_usage
from felix.patterns.types import ChatMessage
from felix.session.compaction import CompactingSessionStrategy
from felix.session.store import InMemorySessionStore
from felix.session.strategies import SummarizingSessionStrategy
from felix.session.tree import annotate_and_append
from felix.session.types import AppendableEvent
from felix.usage import store as usage_store
from felix.usage.pricing import usage_with_cost

WIRE_MODEL = "claude-sonnet-4-5"  # priced in the bundled catalog


@dataclass
class _Route:
    model: str = WIRE_MODEL


class _Summarizer:
    """A route-shaped model: the logical id is what the operator named it, the wire id is
    what the price table keys on. Feeding the logical id to pricing yields $0."""

    model_id = "fast"
    route = _Route()

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages: list[ChatMessage], tools: list[Any], opts: Any = None) -> ModelChatResult:
        self.calls += 1
        return ModelChatResult(
            message=ChatMessage(role="assistant", content="summary"),
            stop_reason="end_turn",
            usage=TokenUsage(input=50_000, output=200),
        )


class _SilentSummarizer(_Summarizer):
    """A provider that reported no usage for the summary."""

    async def chat(self, messages: list[ChatMessage], tools: list[Any], opts: Any = None) -> ModelChatResult:
        self.calls += 1
        return ModelChatResult(
            message=ChatMessage(role="assistant", content="summary"), stop_reason="end_turn"
        )


def _settings() -> Settings:
    return Settings(  # type: ignore[arg-type]
        database_url="memory://summarizer-usage",
        object_store="memory",
        redis_url="",
        allow_insecure=True,
        auth_mode="none",
        host="127.0.0.1",
        environment="development",
    )


def _ctx() -> RequestContext:
    return RequestContext(
        settings=_settings(),
        auth=AuthContext(principal_sub="alice", tenant_id="acme", anonymous=False),
        manifest_id="support",
    )


def _compacting() -> CompactingSessionStrategy:
    return CompactingSessionStrategy(
        reserve_tokens=10, keep_recent_tokens=50, context_window_tokens=100, enabled=True
    )


async def _long_session(store: InMemorySessionStore, thread: str, turns: int = 20):
    session = store.open(thread)
    for i in range(turns):
        await annotate_and_append(
            session,
            [AppendableEvent(kind="message", role="user", content=("hello world " * 200) + str(i))],
        )
    return session


async def _render(strategy: Any, session: Any, model: Any) -> None:
    await strategy.render(
        session, [ChatMessage(role="user", content="continue")], {"system_prompt": "sys", "model": model}
    )


@pytest.fixture(autouse=True)
def _clean_usage_buffer():
    usage_store.pending_buffer().reset_for_tests()
    yield
    usage_store.pending_buffer().reset_for_tests()


def _usage_rows() -> list[dict[str, Any]]:
    return usage_store.pending_buffer().snapshot()


def _expected_cost(tokens: TokenUsage) -> float:
    return usage_with_cost(tokens, model_id=WIRE_MODEL)["cost"]["total"]


@pytest.mark.asyncio
async def test_compaction_bills_the_calling_tenant_and_manifest() -> None:
    store = InMemorySessionStore(tenant_id="acme")
    session = await _long_session(store, "acme:compact")
    model = _Summarizer()
    async with async_run_with_context(_ctx()):
        await _render(_compacting(), session, model)
    assert model.calls == 1
    rows = _usage_rows()
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["tenant_id"] == "acme", "compaction was billed to the literal tenant 'default'"
    assert row["manifest_id"] == "support"
    assert row["meta_json"] == {"kind": "compaction", "reason": "threshold"}
    assert row["tokens_input"] == 50_000


@pytest.mark.asyncio
async def test_compaction_spend_counts_against_the_run_budget_at_the_wire_rate() -> None:
    """`limits.max_cost_usd` and the token caps read `ctx.limit_state`; a summarizer call
    that skipped it was invisible to every budget. The amount is the wire model's rate:
    the logical route name "fast" is not in the price table and contributed $0."""
    store = InMemorySessionStore(tenant_id="acme")
    session = await _long_session(store, "acme:budget")
    ctx = _ctx()
    async with async_run_with_context(ctx):
        await _render(_compacting(), session, _Summarizer())
    assert ctx.limit_state.tokens_input == 50_000
    assert ctx.limit_state.tokens_output == 200
    assert ctx.limit_state.cost_usd == pytest.approx(_expected_cost(TokenUsage(input=50_000, output=200)))
    assert ctx.limit_state.cost_usd > 0


@pytest.mark.asyncio
async def test_compaction_outside_a_request_records_under_no_manifest() -> None:
    """A maintenance path has no request context. The row is still written — a dropped
    meter is worse than an unattributed one — under the anonymous tenant and an empty
    manifest, with `meta.kind` saying what it was. It must not borrow a manifest name."""
    store = InMemorySessionStore(tenant_id="acme")
    session = await _long_session(store, "acme:noctx")
    await _render(_compacting(), session, _Summarizer())
    rows = _usage_rows()
    assert len(rows) == 1
    assert rows[0]["tenant_id"] == "default"
    assert rows[0]["manifest_id"] == ""
    assert rows[0]["meta_json"]["kind"] == "compaction"


@pytest.mark.asyncio
async def test_an_unmetered_summary_is_counted_as_unmetered_not_as_free() -> None:
    """The turn's own path raises `felix_model_unmetered` when a provider reports no
    usage; a summarizer that silently skipped the meter would look like a free call."""
    from felix.observability.metrics import REGISTRY

    labels = {"manifest_id": "support", "model": "fast"}
    before = REGISTRY.get_sample_value("felix_model_unmetered_total", labels) or 0.0
    store = InMemorySessionStore(tenant_id="acme")
    session = await _long_session(store, "acme:silent")
    async with async_run_with_context(_ctx()):
        await _render(_compacting(), session, _SilentSummarizer())
    after = REGISTRY.get_sample_value("felix_model_unmetered_total", labels) or 0.0
    assert after == before + 1
    assert _usage_rows() == []


class _TurnModel:
    """One client serves both the turn and the summary, as in production. A turn is
    cheap; the summary is the 50k-token call, recognisable by `isolate_cache`."""

    model_id = "fast"
    route = _Route()

    def __init__(self) -> None:
        self.summaries = 0
        self.turns = 0

    async def chat(self, messages: Any, tools: Any, opts: Any = None) -> ModelChatResult:
        # The summarizer is the one call that isolates its cache prefix; a turn never does.
        summarizing = bool(getattr(opts, "isolate_cache", False))
        if summarizing:
            self.summaries += 1
        else:
            self.turns += 1
        return ModelChatResult(
            message=ChatMessage(role="assistant", content="summary" if summarizing else "ok"),
            stop_reason="end_turn",
            usage=TokenUsage(input=50_000, output=200) if summarizing else TokenUsage(input=10, output=1),
        )


def _react_agent(store: InMemorySessionStore, model: Any, limits: Any = None) -> Any:
    from felix.patterns.react import _ReactAgent

    agent = _ReactAgent(
        tools=[],
        pattern="react",
        manifest_id="support",
        manifest_version="1",
        system_prompt="s",
        model_spec=None,
        settings=None,
        recursion_limit=3,
        session_store=store,
        session_strategy=_compacting(),
        limits=limits,
    )
    agent._resolve_model = lambda _i: model  # type: ignore[method-assign]
    return agent


@pytest.mark.asyncio
async def test_summarizer_spend_trips_the_run_budget_before_the_turn() -> None:
    """End to end through the loop: the history is rendered (and summarized) first, and
    the budget is checked at the top of the turn — so a summary that alone exceeds
    `max_cost_usd` stops the run before a single turn is bought. Before this the
    summarizer never reached the budget at all."""
    from felix.manifests.schema import Limits
    from felix.patterns.types import InvokeInput

    store = InMemorySessionStore(tenant_id="acme")
    await _long_session(store, "acme:trip")
    model = _TurnModel()
    cap = _expected_cost(TokenUsage(input=50_000, output=200)) / 2  # below one summary
    ctx = _ctx()
    async with async_run_with_context(ctx):
        await _react_agent(store, model, Limits(max_cost_usd=cap)).invoke(
            InvokeInput(messages=[ChatMessage(role="user", content="hi")], thread_id="acme:trip")
        )
    assert model.summaries == 1, "the long history was summarized"
    assert model.turns == 0, "the summary alone spent the budget, so no turn was bought"
    assert ctx.limit_state.aborted is True
    assert "max_cost_usd" in ctx.limit_state.abort_reason


@pytest.mark.asyncio
async def test_the_turn_reports_its_cost_at_the_wire_rate() -> None:
    """The priced block the loop attaches to the turn's event used to be priced by the
    logical route name, so every custom route reported $0 while being budgeted right."""
    from felix.patterns.types import InvokeInput

    store = InMemorySessionStore(tenant_id="acme")
    await _long_session(store, "acme:report")
    model = _TurnModel()
    async with async_run_with_context(_ctx()):
        out = await _react_agent(store, model).invoke(
            InvokeInput(messages=[ChatMessage(role="user", content="hi")], thread_id="acme:report")
        )
    assert out.final is not None and model.turns == 1
    events = await store.open("acme:report").get_events()
    turn = next(e for e in reversed(events) if e.role == "assistant" and (e.metadata or {}).get("usage"))
    assert turn.metadata["usage"]["cost"]["total"] == pytest.approx(
        _expected_cost(TokenUsage(input=10, output=1))
    )
    assert turn.metadata["usage"]["cost"]["total"] > 0


@pytest.mark.asyncio
async def test_the_summarizing_strategy_is_metered_too() -> None:
    """`summarizing:N` recorded nothing at all: a model call with no usage row anywhere."""
    store = InMemorySessionStore(tenant_id="acme")
    session = await _long_session(store, "acme:summ", turns=8)
    model = _Summarizer()
    ctx = _ctx()
    async with async_run_with_context(ctx):
        await _render(SummarizingSessionStrategy(keep=2), session, model)
    assert model.calls == 1
    rows = _usage_rows()
    assert len(rows) == 1
    assert rows[0]["tenant_id"] == "acme"
    assert rows[0]["manifest_id"] == "support"
    assert rows[0]["meta_json"]["kind"] == "summarization"
    assert ctx.limit_state.tokens_input == 50_000


@pytest.mark.asyncio
async def test_the_summarizing_strategy_outside_a_request_records_under_no_manifest() -> None:
    store = InMemorySessionStore(tenant_id="acme")
    session = await _long_session(store, "acme:summ-noctx", turns=8)
    await _render(SummarizingSessionStrategy(keep=2), session, _Summarizer())
    rows = _usage_rows()
    assert len(rows) == 1
    assert rows[0]["manifest_id"] == ""
    assert rows[0]["meta_json"]["kind"] == "summarization"


def test_record_model_usage_prices_by_the_wire_id_and_returns_the_block() -> None:
    """The react loop reports this block on the turn; it used to price it by the logical
    route name and report $0 for every custom route."""
    tokens = TokenUsage(input=1_000_000, output=0)
    result = ModelChatResult(
        message=ChatMessage(role="assistant", content="x"), stop_reason="end_turn", usage=tokens
    )
    block = record_model_usage(result, _Summarizer(), manifest_id="support")
    assert block["cost"]["total"] == pytest.approx(_expected_cost(tokens))
    assert block["cost"]["total"] > 0
    assert _usage_rows()[0]["manifest_id"] == "support"
