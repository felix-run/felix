"""Every model call a composite pattern makes must reach `record_usage`.

`record_usage` is the single feed for `ctx.limit_state.tokens_input/tokens_output/cost_usd`,
which is what `limits.check_budgets` reads. A model call that skips it is not just missing
from the usage table — it is invisible to `limits.max_input_tokens`, `max_output_tokens`,
and `max_cost_usd`, so the declared spend ceiling under-enforces.

`_DelegatingAgent` used to implement every pattern twice, as `_x` and `_stream_x`, and the
copies drifted: the streaming halves of `parallel` and `plan_execute` never recorded usage
at all. They now share one `_run_*` per pattern, but these tests still assert against both
`invoke()` and `stream_events()` — the parametrization is what would catch the split
reopening.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.config import Settings
from felix.context import AuthContext, RequestContext, async_run_with_context
from felix.patterns.model import ModelChatResult, StreamDelta, TokenUsage
from felix.patterns.types import ChatMessage, Event, InvokeInput, InvokeOutput
from felix.usage import store as usage_store


def _settings() -> Settings:
    return Settings(
        database_url="memory://pattern-metering",
        object_store="memory",
        allow_insecure=True,
        environment="development",
    )


def _ctx() -> RequestContext:
    return RequestContext(settings=_settings(), auth=AuthContext(tenant_id="default"))


class _MeteredModel:
    """A provider that streams and reports usage — what the built-in clients do."""

    model_id = "metered"

    def __init__(self, text: str = "synth") -> None:
        self.text = text
        self.calls = 0

    def _result(self) -> ModelChatResult:
        return ModelChatResult(
            message=ChatMessage(role="assistant", content=self.text),
            usage=TokenUsage(input=10, output=4),
        )

    async def chat(self, messages: list[ChatMessage], tools: list) -> ModelChatResult:
        self.calls += 1
        return self._result()

    async def stream_turn(self, messages: list[ChatMessage], tools: list) -> Any:
        self.calls += 1
        yield StreamDelta(kind="text", text=self.text)
        yield self._result()


class _ChatOnlyModel:
    """A plugin-style provider with no `stream_turn` — the un-streamable fallback."""

    model_id = "chat-only"

    def __init__(self, text: str = "synth") -> None:
        self.text = text
        self.calls = 0

    async def chat(self, messages: list[ChatMessage], tools: list) -> ModelChatResult:
        self.calls += 1
        return ModelChatResult(
            message=ChatMessage(role="assistant", content=self.text),
            usage=TokenUsage(input=10, output=4),
        )


class _Child:
    """A sub-agent whose own cost is already accounted for elsewhere."""

    pattern = "react"
    manifest_id = "child"
    manifest_version = "1.0.0"

    def __init__(self, text: str = "note") -> None:
        self.text = text

    async def invoke(self, input: InvokeInput) -> InvokeOutput:
        msg = ChatMessage(role="assistant", content=self.text)
        return InvokeOutput(messages=[*input.messages, msg], final=msg)

    async def stream_events(self, input: InvokeInput) -> Any:
        msg = ChatMessage(role="assistant", content=self.text)
        yield Event(event="text_delta", data={"chunk": {"content": self.text}, "delta": self.text})
        out = InvokeOutput(messages=[*input.messages, msg], final=msg)
        yield Event(event="on_chain_end", data={"output": out})
        yield Event(event="done", data={"final": msg.model_dump()})


def _agent(pattern: str, **kw: Any) -> Any:
    from felix.patterns import delegating

    return delegating._DelegatingAgent(
        tools=[],
        pattern=pattern,
        manifest_id="meter",
        manifest_version="1",
        **kw,
    )


async def _drain(agent: Any, *, streaming: bool) -> None:
    input = InvokeInput(messages=[ChatMessage(role="user", content="hi")])
    if streaming:
        async for _ in agent.stream_events(input):
            pass
    else:
        await agent.invoke(input)


@pytest.mark.parametrize("streaming", [False, True], ids=["invoke", "stream"])
@pytest.mark.asyncio
async def test_parallel_meters_its_synthesis_call(monkeypatch: pytest.MonkeyPatch, streaming: bool) -> None:
    """The aggregator inference is billed on both halves.

    `_stream_parallel` used to route through `_yield_model_stream`, which called
    `model.stream()` and never recorded anything, while `_parallel` recorded once.
    """
    from felix.patterns import delegating

    model = _MeteredModel()
    monkeypatch.setattr(delegating, "_model_for", lambda *a, **k: model)
    agent = _agent("parallel", sub_agents={"a": _Child("one"), "b": _Child("two")})

    ctx = _ctx()
    async with async_run_with_context(ctx):
        await _drain(agent, streaming=streaming)

    assert model.calls == 1, "the synthesis call should happen exactly once"
    assert ctx.limit_state.tokens_input == 10
    assert ctx.limit_state.tokens_output == 4


@pytest.mark.parametrize("streaming", [False, True], ids=["invoke", "stream"])
@pytest.mark.asyncio
async def test_plan_execute_meters_plan_and_synthesis(
    monkeypatch: pytest.MonkeyPatch, streaming: bool
) -> None:
    """Both inferences — the plan and the synthesis — are billed on both halves.

    `_plan_execute` recorded twice; `_stream_plan_execute` recorded only the plan.
    """
    from felix.patterns import delegating

    model = _MeteredModel(text="1. do a thing")
    monkeypatch.setattr(delegating, "_model_for", lambda *a, **k: model)
    agent = _agent("plan_execute", inner=_Child("done"))

    ctx = _ctx()
    async with async_run_with_context(ctx):
        await _drain(agent, streaming=streaming)

    assert model.calls >= 2, "plan and synthesis are separate inferences"
    assert ctx.limit_state.tokens_input == 10 * model.calls
    assert ctx.limit_state.tokens_output == 4 * model.calls


@pytest.mark.asyncio
async def test_streaming_meters_a_provider_that_cannot_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `chat()`-only provider is still billed, and still produces its text.

    `stream()` yields text and nothing else, so usage cannot be recovered from it. Rather
    than stream for show and leave the call uncapped, `_yield_model_stream` falls back to
    one metered `chat()` and emits the answer as a single delta.
    """
    from felix.patterns import delegating

    model = _ChatOnlyModel(text="combined")
    monkeypatch.setattr(delegating, "_model_for", lambda *a, **k: model)
    agent = _agent("parallel", sub_agents={"a": _Child("one")})

    ctx = _ctx()
    async with async_run_with_context(ctx):
        events = [
            ev
            async for ev in agent.stream_events(
                InvokeInput(messages=[ChatMessage(role="user", content="hi")])
            )
        ]

    assert model.calls == 1
    assert ctx.limit_state.tokens_input == 10
    assert "".join(e.text for e in events if e.event == "text_delta").endswith("combined")


@pytest.mark.asyncio
async def test_reflect_verifier_is_metered(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_score` calls the verifier on every iteration and never recorded it."""
    from felix.patterns import delegating

    model = _MeteredModel(text="0.95")
    monkeypatch.setattr(delegating, "build_model", lambda *a, **k: model)

    agent = _agent("reflect", inner=_Child("draft"))
    ctx = _ctx()
    async with async_run_with_context(ctx):
        await agent.invoke(InvokeInput(messages=[ChatMessage(role="user", content="hi")]))

    assert model.calls >= 1, "the verifier was called"
    assert ctx.limit_state.tokens_input == 10 * model.calls


@pytest.mark.asyncio
async def test_streamed_usage_reaches_the_usage_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """Metering is not only a limit concern — the billing row must exist too."""
    from felix.patterns import delegating

    usage_store.clear_memory()
    model = _MeteredModel()
    monkeypatch.setattr(delegating, "_model_for", lambda *a, **k: model)
    agent = _agent("parallel", sub_agents={"a": _Child("one")})

    async with async_run_with_context(_ctx()):
        await _drain(agent, streaming=True)

    assert usage_store.pending_count() == 1
