"""A queued follow-up is answered by an ordinary step of the react loop.

It used to be a bare `model.chat` after the loop: a tool call in its reply was appended and
never run (the next run then closed it as "may have already taken effect"), a streamed run got
the answer as one lump, and the run reported the *previous* turn's stop reason. Each test here
fails on that path.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.config import Settings
from felix.context import AuthContext, RequestContext, async_run_with_context
from felix.hooks import get_agent_hooks, reset_agent_hooks
from felix.manifests.schema import ModelSpec
from felix.patterns.react import _ReactAgent
from felix.patterns.types import ChatMessage, InvokeInput, InvokeOutput, ToolCall
from felix.steer import drain_follow_up, enqueue, release_run_queue
from felix.tools.types import Tool, ToolInput, ToolInvocationCtx, ToolOutput
from felix_ai.types import ModelChatResult, StreamDelta, TokenUsage

TENANT = "default"


def _reply(
    content: str = "", *, calls: list[ToolCall] | None = None, stop: str = "end_turn"
) -> ModelChatResult:
    return ModelChatResult(
        message=ChatMessage(role="assistant", content=content, tool_calls=calls or []),
        stop_reason=stop,  # type: ignore[arg-type]
        usage=TokenUsage(),
    )


class _Script:
    """Plays `replies` in order and records, per call, how it was reached and what it was sent."""

    model_id = "follow-up-fake"

    def __init__(self, *replies: ModelChatResult) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[str, list[str]]] = []

    def _next(self, how: str, messages: list[ChatMessage]) -> ModelChatResult:
        self.calls.append((how, [str(m.content) for m in messages if m.role == "user"]))
        # The last reply repeats, so a run that makes more calls than scripted fails on what it
        # asserts rather than on an IndexError here.
        return self.replies[min(len(self.calls), len(self.replies)) - 1]

    async def chat(self, messages: list[ChatMessage], tools: list, opts: Any = None) -> ModelChatResult:
        return self._next("chat", messages)


class _StreamingScript(_Script):
    async def stream_turn(self, messages: list[ChatMessage], tools: list, opts: Any = None) -> Any:
        result = self._next("stream_turn", messages)
        for word in (result.message.content or "").split():
            yield StreamDelta(kind="text", text=word + " ")
        yield result


class _Counter:
    transport = "local"

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, args: ToolInput, ctx: ToolInvocationCtx | None = None) -> ToolOutput:
        self.calls += 1
        return "looked it up"


def _agent(model: _Script, tool: _Counter | None = None, **kw: Any) -> _ReactAgent:
    tools = [Tool(name="lookup", description="d", args_schema=None, executor=tool)] if tool else []
    agent = _ReactAgent(
        tools=tools,
        pattern="react",
        manifest_id="follow-up",
        manifest_version="1",
        system_prompt="s",
        model_spec=ModelSpec(id="follow-up-fake"),
        settings=None,
        recursion_limit=kw.pop("recursion_limit", 10),
        **kw,
    )
    agent._resolve_model = lambda _input: model  # type: ignore[method-assign]
    return agent


def _ctx() -> RequestContext:
    settings = Settings(
        allow_insecure=True, auth_mode="none", environment="development", database_url="memory://follow-up"
    )
    return RequestContext(settings=settings, auth=AuthContext(tenant_id=TENANT, scopes=frozenset()))


async def _run(agent: _ReactAgent, thread: str, *follow_ups: str, stream: bool = False) -> tuple[Any, list]:
    for text in follow_ups:
        await enqueue(TENANT, thread, kind="follow_up", text=text)
    input = InvokeInput(
        messages=[ChatMessage(role="user", content="first")], tenant_id=TENANT, thread_id=thread
    )
    events: list = []
    try:
        async with async_run_with_context(_ctx()):
            if not stream:
                return await agent.invoke(input), events
            out = None
            async for ev in agent.stream_events(input):
                events.append(ev)
                if ev.event == "on_chain_end":
                    out = ev.data["output"]
            return out, events
    finally:
        await drain_follow_up(TENANT, thread)
        await release_run_queue(TENANT, thread)


@pytest.mark.asyncio
async def test_a_tool_call_in_a_follow_ups_reply_runs() -> None:
    tool = _Counter()
    model = _Script(
        _reply("first answer"),
        _reply(calls=[ToolCall(id="c1", name="lookup", args={})], stop="tool_use"),
        _reply("answer using the lookup"),
    )
    out, _ = await _run(_agent(model, tool), "default:fu-tool", "look it up")

    assert tool.calls == 1, "the follow-up's tool call was never executed"
    assert out.final.content == "answer using the lookup"
    assert [m.role for m in out.messages[-4:]] == ["user", "assistant", "tool", "assistant"]


@pytest.mark.asyncio
async def test_a_streamed_follow_up_turn_streams() -> None:
    model = _StreamingScript(_reply("first answer"), _reply("three word reply"))
    out, events = await _run(_agent(model), "default:fu-stream", "and then?", stream=True)

    assert [how for how, _ in model.calls] == ["stream_turn", "stream_turn"]
    deltas = [e.data["delta"] for e in events if e.event == "text_delta"]
    assert deltas[-3:] == ["three ", "word ", "reply "], "the follow-up's answer arrived as one lump"
    assert [e.data["content"] for e in events if e.event == "follow_up"] == ["and then?"]
    assert out.final.content == "three word reply"


@pytest.mark.asyncio
async def test_the_run_reports_the_follow_up_turns_stop_reason() -> None:
    model = _Script(_reply("first answer"), _reply("cut off mid", stop="max_tokens"))
    out, _ = await _run(_agent(model), "default:fu-stop", "go on")

    assert out.stop_reason == "max_tokens", "the run reported the first turn's end_turn"


@pytest.mark.asyncio
async def test_all_mode_answers_every_queued_follow_up_in_one_turn() -> None:
    model = _Script(_reply("first answer"), _reply("both handled"))
    out, _ = await _run(_agent(model, follow_up_mode="all"), "default:fu-all", "one", "two")

    assert len(model.calls) == 2
    assert model.calls[1][1][-2:] == ["one", "two"]
    assert out.final.content == "both handled"


@pytest.mark.asyncio
async def test_one_at_a_time_mode_answers_each_in_its_own_turn() -> None:
    model = _Script(_reply("first answer"), _reply("handled one"), _reply("handled two"))
    out, _ = await _run(_agent(model, follow_up_mode="one-at-a-time"), "default:fu-one", "one", "two")

    assert [users[-1] for _, users in model.calls] == ["first", "one", "two"]
    assert out.final.content == "handled two"


@pytest.mark.asyncio
async def test_a_run_with_no_step_left_leaves_the_follow_up_queued() -> None:
    thread = "default:fu-no-step"
    model = _Script(_reply("first answer"), _reply("should not be asked"))
    await enqueue(TENANT, thread, kind="follow_up", text="later")
    try:
        async with async_run_with_context(_ctx()):
            out: InvokeOutput = await _agent(model, recursion_limit=1).invoke(
                InvokeInput(
                    messages=[ChatMessage(role="user", content="first")], tenant_id=TENANT, thread_id=thread
                )
            )
        left = await drain_follow_up(TENANT, thread)
    finally:
        await release_run_queue(TENANT, thread)

    assert len(model.calls) == 1
    assert out.stop_reason == "end_turn", "a run that answered on its last step is not max_turns"
    assert [m.text for m in left] == ["later"], "the follow-up was dropped instead of kept for the next run"


@pytest.mark.asyncio
async def test_a_follow_up_is_answered_after_a_terminal_tool_too() -> None:
    """A terminal tool is the loop's other idle exit; a follow-up queued for it is still answered."""
    reset_agent_hooks()
    get_agent_hooks().register_after_tool(lambda call, result, is_error, ctx: {"terminate": True})
    tool = _Counter()
    model = _Script(
        _reply(calls=[ToolCall(id="c1", name="lookup", args={})], stop="tool_use"), _reply("after")
    )
    try:
        out, _ = await _run(_agent(model, tool), "default:fu-terminal", "one more thing")
    finally:
        reset_agent_hooks()

    assert tool.calls == 1
    assert [users[-1] for _, users in model.calls] == ["first", "one more thing"]
    assert out.final.content == "after"
