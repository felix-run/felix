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
from felix.manifests.schema import ReflectSpec
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
        # Usage varies per call, so a total identifies *which* calls were recorded.
        # With a flat 10/4 for every call, `tokens_input == 10 * calls` was satisfied by
        # any assignment of N records across N calls — including billing the planning
        # inference twice and the synthesis not at all, which is the exact defect this
        # file exists to catch.
        return ModelChatResult(
            message=ChatMessage(role="assistant", content=self.text),
            usage=TokenUsage(input=10 * self.calls, output=4 * self.calls),
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

    assert model.calls == 2, "plan and synthesis are separate inferences, and only those"
    # 10 + 20 and 4 + 8: both calls, each exactly once. A flat total would not say that.
    assert ctx.limit_state.tokens_input == 30
    assert ctx.limit_state.tokens_output == 12


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
    # Exactly one delta: the documented trade is one metered `chat()` emitted whole,
    # not token-by-token. `endswith` passed on any number of deltas and so pinned neither.
    assert [e.text for e in events if e.event == "text_delta"] == ["combined"]


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

    assert model.calls == 1, "the verifier is called once; 0.95 clears the threshold"
    assert ctx.limit_state.tokens_input == 10


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


class _ThinkingModel(_MeteredModel):
    """A provider whose answer carries extended-thinking blocks, as Anthropic's does."""

    def _result(self) -> ModelChatResult:
        return ModelChatResult(
            message=ChatMessage(
                role="assistant",
                content=self.text,
                thinking=[{"type": "thinking", "thinking": "weighing the specialists"}],
            ),
            usage=TokenUsage(input=10 * self.calls, output=4 * self.calls),
        )


@pytest.mark.parametrize("pattern", ["parallel", "plan_execute"])
@pytest.mark.asyncio
async def test_invoke_keeps_the_thinking_on_the_synthesized_answer(
    monkeypatch: pytest.MonkeyPatch, pattern: str
) -> None:
    """Unifying the two halves must not adopt the streaming half's lossy shape.

    `_parallel` and `_plan_execute` returned `final=synth.message` — the model's own
    `ChatMessage`, thinking blocks and all, which `session/types.py` persists and replays.
    Their streaming twins rebuilt it from collected text because that is all
    `model.stream()` yields. Collapsing the pair onto one implementation is only correct
    if the non-streaming path keeps what it had; streaming stays lossy because the wire
    gives it nothing else.
    """
    from felix.patterns import delegating

    model = _ThinkingModel(text="1. step" if pattern == "plan_execute" else "synth")
    monkeypatch.setattr(delegating, "_model_for", lambda *a, **k: model)
    kw: dict[str, Any] = (
        {"inner": _Child("done")} if pattern == "plan_execute" else {"sub_agents": {"a": _Child("one")}}
    )
    agent = _agent(pattern, **kw)

    async with async_run_with_context(_ctx()):
        out = await agent.invoke(InvokeInput(messages=[ChatMessage(role="user", content="hi")]))

    assert out.final.thinking, "the synthesized answer lost its thinking blocks"
    assert out.final.content == model.text


@pytest.mark.parametrize("streaming", [False, True], ids=["invoke", "stream"])
@pytest.mark.asyncio
async def test_router_meters_its_routing_call(monkeypatch: pytest.MonkeyPatch, streaming: bool) -> None:
    """`_choose_child` is an inference too, on both halves.

    `groupchat` deliberately has no equivalent test: it makes no model call of its own,
    only delegating to children that meter themselves.
    """
    from felix.patterns import delegating

    model = _MeteredModel(text="a")
    monkeypatch.setattr(delegating, "_model_for", lambda *a, **k: model)
    agent = _agent("router", sub_agents={"a": _Child("one"), "b": _Child("two")})

    ctx = _ctx()
    async with async_run_with_context(ctx):
        await _drain(agent, streaming=streaming)

    assert model.calls == 1
    assert ctx.limit_state.tokens_input == 10


class _ScriptedVerifier:
    """Returns a scripted score per call, so the retry loop can be driven."""

    model_id = "verifier"

    def __init__(self, *scores: str) -> None:
        self.scores = list(scores)
        self.calls = 0

    async def chat(self, messages: list[ChatMessage], tools: list) -> ModelChatResult:
        self.calls += 1
        text = self.scores.pop(0) if self.scores else "1.0"
        return ModelChatResult(
            message=ChatMessage(role="assistant", content=text),
            usage=TokenUsage(input=1, output=1),
        )


class _CountingChild(_Child):
    """A base agent that records how many drafts it was asked for."""

    def __init__(self, text: str = "draft") -> None:
        super().__init__(text)
        self.invocations = 0
        self.seen: list[str] = []

    async def invoke(self, input: InvokeInput) -> InvokeOutput:
        self.invocations += 1
        self.seen.append(input.messages[-1].content)
        return await super().invoke(input)


@pytest.mark.asyncio
async def test_a_low_score_drives_another_pass_with_a_critique(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The payoff of failing closed: a bad score costs a retry, not a rubber stamp.

    Asserted end to end rather than only at `_score`, because the retry is what a verifier
    outage now buys — and nothing covered the loop body at all.
    """
    from felix.patterns import delegating

    verifier = _ScriptedVerifier("0.1", "0.95")
    monkeypatch.setattr(delegating, "build_model", lambda *a, **k: verifier)

    base = _CountingChild("draft")
    agent = _agent("reflect", inner=base)
    agent.reflect_cfg = ReflectSpec(max_iterations=3, threshold=0.7, criteria="assert_present:zebra")

    async with async_run_with_context(_ctx()):
        await agent.invoke(InvokeInput(messages=[ChatMessage(role="user", content="hi")]))

    assert base.invocations == 2, "0.1 is below threshold, so a second pass is required"
    assert "Improve against: assert_present:zebra" in base.seen[1]
    assert "scored 0.10" in base.seen[1]
    # Two scores, not one: the second draft is scored too, and 0.95 is what breaks the
    # loop before the third iteration the config would otherwise allow.
    assert verifier.calls == 2


@pytest.mark.asyncio
async def test_an_unparseable_reply_is_still_billed(monkeypatch: pytest.MonkeyPatch) -> None:
    """ "The verifier is billed whether or not its reply parses" — asserted, not asserted-to."""
    from felix.patterns import delegating

    model = _MeteredModel(text="I cannot score this")
    monkeypatch.setattr(delegating, "build_model", lambda *a, **k: model)

    ctx = _ctx()
    async with async_run_with_context(ctx):
        score = await _agent("reflect")._score("some answer", "assert_present:zebra", "")

    assert score == 0.0, "an unparseable reply falls through to the heuristic"
    assert ctx.limit_state.tokens_input == 10, "and the call is billed anyway"
