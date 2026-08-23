"""A streaming turn used to run the whole inference twice.

`stream_events` streamed a turn for display and then called `chat()` for the real
answer. Two full inferences for one turn: the input was billed twice, only the second
was metered — so `limits.max_cost_usd` and the token budgets counted roughly half of
what a streaming run actually spent — and the answer was sampled twice, so the text a
user watched arrive could differ from the text that was saved. The streamed request also
carried no tools, which is why the second call was needed at all.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.patterns.model import ModelChatResult, StreamDelta, TokenUsage
from felix.patterns.react import _ReactAgent
from felix.patterns.types import ChatMessage, InvokeInput, ToolCall
from felix.tools.types import define_tool


class _SingleCallModel:
    """Implements `stream_turn`, like the built-in HTTP client."""

    model_id = "scripted"

    def __init__(self, result: ModelChatResult, deltas: list[StreamDelta] | None = None) -> None:
        self._result = result
        self._followup = ModelChatResult(
            message=ChatMessage(role="assistant", content="done"),
            stop_reason="end_turn",
            usage=TokenUsage(),
        )
        self._served = False
        self._deltas = deltas or [StreamDelta(kind="text", text="Hel"), StreamDelta(kind="text", text="lo")]
        self.stream_turn_calls = 0
        self.stream_calls = 0
        self.chat_calls = 0

    async def stream_turn(self, messages: list[ChatMessage], tools: list[Any], opts: Any = None):
        self.stream_turn_calls += 1
        if self._served:
            yield self._followup
            return
        self._served = True
        for d in self._deltas:
            yield d
        yield self._result

    async def stream(self, messages: list[ChatMessage], tools: list[Any], opts: Any = None):
        self.stream_calls += 1
        yield "unused"

    async def chat(self, messages: list[ChatMessage], tools: list[Any], opts: Any = None):
        self.chat_calls += 1
        return self._result


class _LegacyModel:
    """Only implements `stream()`, as a plugin-supplied client may."""

    model_id = "legacy"

    def __init__(self) -> None:
        self.stream_calls = 0
        self.chat_calls = 0

    async def stream(self, messages: list[ChatMessage], tools: list[Any], opts: Any = None):
        self.stream_calls += 1
        yield "Hello"

    async def chat(self, messages: list[ChatMessage], tools: list[Any], opts: Any = None):
        self.chat_calls += 1
        return ModelChatResult(
            message=ChatMessage(role="assistant", content="Hello"),
            stop_reason="end_turn",
            usage=TokenUsage(input=10, output=5),
        )


def _agent(model: Any, tools: list[Any] | None = None) -> _ReactAgent:
    agent = _ReactAgent(
        tools=tools or [],
        pattern="react",
        manifest_id="test",
        manifest_version="1",
        system_prompt="s",
        model_spec=None,
        settings=None,
        recursion_limit=3,
    )
    agent._resolve_model = lambda _input: model  # type: ignore[method-assign]
    return agent


async def _drain(agent: _ReactAgent) -> list[Any]:
    return [
        e async for e in agent.stream_events(InvokeInput(messages=[ChatMessage(role="user", content="hi")]))
    ]


@pytest.mark.asyncio
async def test_streaming_turn_makes_exactly_one_model_call() -> None:
    model = _SingleCallModel(
        ModelChatResult(
            message=ChatMessage(role="assistant", content="Hello"),
            stop_reason="end_turn",
            usage=TokenUsage(input=1000, output=50),
        )
    )
    await _drain(_agent(model))
    assert model.stream_turn_calls == 1
    assert model.chat_calls == 0, "the second inference is what doubled the bill"
    assert model.stream_calls == 0


@pytest.mark.asyncio
async def test_streamed_text_is_the_text_that_is_kept() -> None:
    """Two samplings could disagree; one cannot."""
    model = _SingleCallModel(
        ModelChatResult(
            message=ChatMessage(role="assistant", content="Hello"),
            stop_reason="end_turn",
            usage=TokenUsage(),
        )
    )
    events = await _drain(_agent(model))
    streamed = "".join(e.data["delta"] for e in events if e.event == "text_delta")
    assert streamed == "Hello"


@pytest.mark.asyncio
async def test_tool_calls_arrive_from_the_streamed_turn() -> None:
    executed: list[str] = []

    async def _handler(args: dict[str, Any], ctx: Any = None) -> str:
        executed.append(str(args.get("q") or ""))
        return "ok"

    tool = define_tool(name="search", description="search", handler=_handler)
    model = _SingleCallModel(
        ModelChatResult(
            message=ChatMessage(
                role="assistant",
                content="looking",
                tool_calls=[ToolCall(id="c1", name="search", args={"q": "felix"})],
            ),
            stop_reason="tool_use",
            usage=TokenUsage(),
        )
    )
    await _drain(_agent(model, [tool]))
    assert executed == ["felix"], "the streamed request now carries tools"


@pytest.mark.asyncio
async def test_thinking_deltas_are_surfaced_but_not_counted_as_text() -> None:
    model = _SingleCallModel(
        ModelChatResult(
            message=ChatMessage(role="assistant", content="answer"),
            stop_reason="end_turn",
            usage=TokenUsage(),
        ),
        deltas=[StreamDelta(kind="thinking", text="hmm"), StreamDelta(kind="text", text="answer")],
    )
    events = await _drain(_agent(model))
    kinds = [
        e.data["progress"]["kind"]
        for e in events
        if e.event == "session_progress" and e.data.get("progress", {}).get("type") == "assistant_delta"
    ]
    assert "thinking" in kinds
    text = "".join(e.data["delta"] for e in events if e.event == "text_delta")
    assert text == "answer", "thinking is not part of the answer body"


@pytest.mark.asyncio
async def test_provider_without_stream_turn_still_works() -> None:
    """Plugin clients that only implement `stream()` keep the old two-call behaviour."""
    model = _LegacyModel()
    events = await _drain(_agent(model))
    assert model.stream_calls == 1
    assert model.chat_calls == 1, "no single-call path available, so the turn still costs two"
    assert any(e.event == "text_delta" for e in events)
