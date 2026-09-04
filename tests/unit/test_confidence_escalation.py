"""`confidence_escalation` is a published manifest field that had no test at all.

`spec.model.confidence_escalation` is in `manifests/schema.py` and in the generated JSON
Schema every manifest's editor reads, and `rg escalat tests/` found only prose. The gap
mattered: escalation makes **two** billed calls and returns the second, and callers meter
what they are given — `record_usage(result, ...)` in `react` and `delegating` only ever sees
the returned result. The primary's tokens reached neither `ctx.limit_state` nor the usage
table, so `limits.max_cost_usd` under-counted an escalating run by whatever the discarded
turn cost. Measured before the fix: 2,100 tokens billed, 100 metered.

That is the "billed twice, metered once" shape `tests/unit/test_invariants.py` cites as
having shipped twice already, and the composite is the only place that can see both turns.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.patterns.model_composites import _EscalationClient
from felix_ai.types import ChatMessage, ModelChatResult, ModelRoute, StreamDelta, TokenUsage, ToolCall

ROUTE = ModelRoute(provider="anthropic", model="m")


class _Model:
    def __init__(self, name: str, text: str, usage: TokenUsage | None, **kw: Any) -> None:
        self.model_id = name
        self.route = ROUTE
        self._text = text
        self._usage = usage
        self._tool_calls = kw.get("tool_calls")
        self.calls: list[str] = []

    async def chat(self, messages: Any, tools: Any, opts: Any = None) -> ModelChatResult:
        self.calls.append("chat")
        return ModelChatResult(
            message=ChatMessage(role="assistant", content=self._text, tool_calls=self._tool_calls),
            usage=self._usage,
        )

    async def stream(self, messages: Any, tools: Any, opts: Any = None) -> Any:
        yield self._text


def _escalating(primary: _Model, target: _Model) -> _EscalationClient:
    return _EscalationClient(
        primary=primary,
        escalate_to=target,
        markers=["not sure"],
        min_response_chars=0,
        model_id=primary.model_id,
        route=ROUTE,
    )


@pytest.mark.asyncio
async def test_an_escalated_turn_meters_both_calls() -> None:
    """The regression: the discarded turn was billed and invisible to every budget."""
    primary = _Model("weak", "I am not sure", TokenUsage(input=1000, output=1000))
    target = _Model("strong", "the confident answer", TokenUsage(input=50, output=50))

    result = await _escalating(primary, target).chat([], [])

    assert primary.calls == ["chat"] and target.calls == ["chat"], "both turns were billed"
    assert result.message.content == "the confident answer", "the better answer is returned"
    assert result.usage is not None
    assert (result.usage.input, result.usage.output) == (1050, 1050), (
        "the primary's tokens must reach the caller's record_usage, or limits.max_cost_usd "
        "under-counts an escalating run by whatever the discarded turn cost"
    )


@pytest.mark.asyncio
async def test_cache_tokens_are_summed_too() -> None:
    """Cache reads are a real cost line for Anthropic and are counted by usage_with_cost."""
    primary = _Model("weak", "not sure", TokenUsage(input=1, output=1, cache_read=700, cache_creation=9))
    target = _Model("strong", "answer", TokenUsage(input=2, output=2, cache_read=3, cache_creation=1))

    result = await _escalating(primary, target).chat([], [])

    assert result.usage is not None
    assert result.usage.cache_read == 703
    assert result.usage.cache_creation == 10


@pytest.mark.asyncio
async def test_a_confident_answer_costs_one_call() -> None:
    primary = _Model("weak", "a clear and complete answer", TokenUsage(input=10, output=10))
    target = _Model("strong", "unused", TokenUsage(input=999, output=999))

    result = await _escalating(primary, target).chat([], [])

    assert target.calls == [], "escalation is for low confidence, not for every turn"
    assert result.usage is not None and result.usage.input == 10, "and meters only what it spent"


@pytest.mark.asyncio
async def test_tool_calls_are_never_escalated() -> None:
    """A turn that wants a tool has not answered yet; its text is not low confidence."""
    primary = _Model(
        "weak",
        "",
        TokenUsage(input=10, output=10),
        tool_calls=[ToolCall(id="1", name="calculator", args={})],
    )
    target = _Model("strong", "unused", TokenUsage(input=999, output=999))

    result = await _escalating(primary, target).chat([], [])

    assert target.calls == [], "escalating here would discard the tool call"
    assert result.message.tool_calls


@pytest.mark.asyncio
async def test_a_missing_usage_on_either_turn_does_not_erase_the_other() -> None:
    """A provider that reports no usage must not silently zero what the other turn cost."""
    primary = _Model("weak", "not sure", TokenUsage(input=1000, output=1000))
    target = _Model("strong", "answer", None)
    result = await _escalating(primary, target).chat([], [])
    assert result.usage is not None and result.usage.input == 1000

    primary_none = _Model("weak", "not sure", None)
    target_usage = _Model("strong", "answer", TokenUsage(input=7, output=7))
    result = await _escalating(primary_none, target_usage).chat([], [])
    assert result.usage is not None and result.usage.input == 7


@pytest.mark.asyncio
async def test_the_streamed_path_carries_the_same_metered_result() -> None:
    """Escalation cannot stream — it needs the finished reply to judge confidence — so it
    settles the turn and chunks it. The terminal result is what the caller meters."""
    primary = _Model("weak", "I am not sure", TokenUsage(input=1000, output=1000))
    target = _Model("strong", "the confident answer", TokenUsage(input=50, output=50))

    emitted = [item async for item in _escalating(primary, target).stream_turn([], [])]

    text = "".join(i.text for i in emitted if isinstance(i, StreamDelta))
    assert text == "the confident answer"
    finals = [i for i in emitted if isinstance(i, ModelChatResult)]
    assert len(finals) == 1 and finals[0].usage is not None
    assert finals[0].usage.input == 1050, "the streamed path must meter both turns too"
