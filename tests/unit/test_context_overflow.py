"""Compaction is threshold-driven, so a provider rejection was a dead end.

The trigger is an estimate: characters over four, anchored on the last reported usage.
It runs slightly behind the truth, and a manifest can declare a window larger than the
model really has. When the estimate is optimistic the provider rejects the request — and
that rejection was a hard failure for the run rather than the most accurate signal yet
that the conversation needs compacting.

Two providers do not reject at all. One accepts the request and reports more input than
the window holds; another truncates and returns a length-stop having produced nothing.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.patterns.model import ModelChatResult, ModelGatewayError, TokenUsage
from felix.patterns.overflow import is_context_overflow, is_silent_overflow
from felix.patterns.react import _ReactAgent
from felix.patterns.types import ChatMessage, InvokeInput


def _err(body: str, status: int = 400) -> ModelGatewayError:
    return ModelGatewayError("anthropic", status, body)


# --- classifying the rejection --------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        '{"error":{"code":"context_length_exceeded"}}',
        "This model's maximum context length is 200000 tokens",
        "prompt is too long: 250000 tokens > 200000 maximum",
        "input length and `max_tokens` exceed context limit",
        "Please reduce the length of the messages",
    ],
)
def test_overflow_phrasings_are_recognised(body: str) -> None:
    assert is_context_overflow(_err(body))


@pytest.mark.parametrize(
    "body",
    [
        "Rate limit reached for tokens per min (TPM)",
        '{"error":{"type":"rate_limit_error"}}',
        "You exceeded your current quota",
        '{"error":{"type":"overloaded_error"}}',
        "Server at capacity, please retry",
    ],
)
def test_throttling_is_not_mistaken_for_overflow(body: str) -> None:
    """Throttling mentions tokens and limits too. Compacting here would discard history
    to fix a problem a plain retry solves."""
    assert not is_context_overflow(_err(body, status=429))


def test_unrelated_errors_are_not_overflow() -> None:
    assert not is_context_overflow(_err("invalid api key", status=401))
    assert not is_context_overflow(_err("", status=500))
    assert not is_context_overflow(RuntimeError("boom"))


def test_a_body_that_cannot_be_read_is_not_overflow() -> None:
    class _Hostile:
        @property
        def body(self) -> str:
            raise RuntimeError("consumed")

    assert not is_context_overflow(_Hostile())


# --- the silent shapes ----------------------------------------------------------


def test_reported_input_over_the_window_is_an_overflow() -> None:
    assert is_silent_overflow(
        stop_reason="end_turn", tokens_input=260_000, tokens_output=10, context_window=200_000
    )


def test_length_stop_with_no_output_is_an_overflow() -> None:
    """A model that emits nothing before hitting its ceiling had no room to answer."""
    assert is_silent_overflow(
        stop_reason="max_tokens", tokens_input=199_000, tokens_output=0, context_window=200_000
    )


def test_a_normal_truncated_answer_is_not_an_overflow() -> None:
    """Stopping on length having produced output is ordinary truncation."""
    assert not is_silent_overflow(
        stop_reason="max_tokens", tokens_input=1_000, tokens_output=4_096, context_window=200_000
    )


def test_a_healthy_turn_is_not_an_overflow() -> None:
    assert not is_silent_overflow(
        stop_reason="end_turn", tokens_input=1_000, tokens_output=50, context_window=200_000
    )


def test_an_unknown_window_does_not_manufacture_an_overflow() -> None:
    assert not is_silent_overflow(
        stop_reason="end_turn", tokens_input=999_999, tokens_output=5, context_window=0
    )


# --- recovery in the loop -------------------------------------------------------


class _OverflowThenOk:
    model_id = "claude-sonnet-4-5"

    def __init__(self, error_body: str) -> None:
        self._body = error_body
        self.chat_calls = 0

    async def chat(self, messages: list[ChatMessage], tools: list[Any], opts: Any = None):
        self.chat_calls += 1
        if self.chat_calls == 1:
            raise _err(self._body)
        return ModelChatResult(
            message=ChatMessage(role="assistant", content="recovered"),
            stop_reason="end_turn",
            usage=TokenUsage(input=100, output=10),
        )


class _AlwaysOverflows:
    model_id = "claude-sonnet-4-5"

    def __init__(self) -> None:
        self.chat_calls = 0

    async def chat(self, messages: list[ChatMessage], tools: list[Any], opts: Any = None):
        self.chat_calls += 1
        raise _err("prompt is too long")


class _Strategy:
    """Stands in for CompactingSessionStrategy."""

    def __init__(self) -> None:
        self.compactions: list[str] = []

    async def compact_now(self, session: Any, **kw: Any) -> dict[str, Any]:
        self.compactions.append(str(kw.get("reason") or ""))
        return {"ok": True}

    async def render(self, session: Any, incoming: list[ChatMessage], opts: Any) -> list[ChatMessage]:
        return [ChatMessage(role="user", content="compacted history")]


class _Store:
    def open(self, thread_id: str) -> object:
        return object()


def _agent(model: Any, strategy: Any | None, store: Any | None) -> _ReactAgent:
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
        session_strategy=strategy,
    )
    agent._resolve_model = lambda _i: model  # type: ignore[method-assign]
    return agent


@pytest.mark.asyncio
async def test_overflow_compacts_and_retries_once() -> None:
    model = _OverflowThenOk("prompt is too long")
    strategy = _Strategy()
    out = await _agent(model, strategy, _Store()).invoke(
        InvokeInput(messages=[ChatMessage(role="user", content="hi")], thread_id="t1")
    )
    assert strategy.compactions == ["overflow"]
    assert model.chat_calls == 2, "compact, then try again exactly once"
    assert out.final.content == "recovered"


@pytest.mark.asyncio
async def test_a_second_overflow_propagates() -> None:
    """Compacting twice against a request that still will not fit is a loop."""
    model = _AlwaysOverflows()
    with pytest.raises(ModelGatewayError):
        await _agent(model, _Strategy(), _Store()).invoke(
            InvokeInput(messages=[ChatMessage(role="user", content="hi")], thread_id="t1")
        )
    assert model.chat_calls == 2


@pytest.mark.asyncio
async def test_throttling_is_not_recovered_by_compacting() -> None:
    model = _OverflowThenOk("rate limit reached for tokens per min")
    strategy = _Strategy()
    with pytest.raises(ModelGatewayError):
        await _agent(model, strategy, _Store()).invoke(
            InvokeInput(messages=[ChatMessage(role="user", content="hi")], thread_id="t1")
        )
    assert strategy.compactions == [], "history must not be discarded over backpressure"


@pytest.mark.asyncio
async def test_without_a_session_the_error_propagates() -> None:
    """There is nothing to compact, so there is nothing to retry."""
    model = _OverflowThenOk("prompt is too long")
    with pytest.raises(ModelGatewayError):
        await _agent(model, None, None).invoke(InvokeInput(messages=[ChatMessage(role="user", content="hi")]))
    assert model.chat_calls == 1


class _SilentThenOk:
    """Accepts the request and reports more input than the window holds."""

    model_id = "claude-sonnet-4-5"  # 200K window in the catalog

    def __init__(self) -> None:
        self.chat_calls = 0

    async def chat(self, messages: list[ChatMessage], tools: list[Any], opts: Any = None):
        self.chat_calls += 1
        if self.chat_calls == 1:
            return ModelChatResult(
                message=ChatMessage(role="assistant", content="truncated nonsense"),
                stop_reason="end_turn",
                usage=TokenUsage(input=260_000, output=5),
            )
        return ModelChatResult(
            message=ChatMessage(role="assistant", content="recovered"),
            stop_reason="end_turn",
            usage=TokenUsage(input=1_000, output=5),
        )


@pytest.mark.asyncio
async def test_silent_overflow_is_recovered_even_though_nothing_raised() -> None:
    model = _SilentThenOk()
    strategy = _Strategy()
    out = await _agent(model, strategy, _Store()).invoke(
        InvokeInput(messages=[ChatMessage(role="user", content="hi")], thread_id="t1")
    )
    assert strategy.compactions == ["overflow_silent"]
    assert out.final.content == "recovered"


class _StreamOverflowThenOk:
    model_id = "claude-sonnet-4-5"

    def __init__(self) -> None:
        self.turns = 0

    async def stream_turn(self, messages: list[ChatMessage], tools: list[Any], opts: Any = None):
        from felix.patterns.model import StreamDelta

        self.turns += 1
        if self.turns == 1:
            raise _err("prompt is too long")
        yield StreamDelta(kind="text", text="recovered")
        yield ModelChatResult(
            message=ChatMessage(role="assistant", content="recovered"),
            stop_reason="end_turn",
            usage=TokenUsage(input=100, output=5),
        )

    async def chat(self, messages: list[ChatMessage], tools: list[Any], opts: Any = None):
        raise AssertionError("stream_turn should have produced the result")


@pytest.mark.asyncio
async def test_streaming_overflow_recovers_before_anything_ships() -> None:
    model = _StreamOverflowThenOk()
    strategy = _Strategy()
    agent = _agent(model, strategy, _Store())
    events = [
        e
        async for e in agent.stream_events(
            InvokeInput(messages=[ChatMessage(role="user", content="hi")], thread_id="t1")
        )
    ]
    assert strategy.compactions == ["overflow"]
    assert model.turns == 2
    text = "".join(e.data["delta"] for e in events if e.event == "text_delta")
    assert text == "recovered", "the client sees one answer, not two spliced together"
