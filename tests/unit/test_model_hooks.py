"""`before_model` / `after_model` hooks, driven through the react loop rather than their runners.

A runner that returns the right list proves nothing if the loop still sends `[*messages,
*transient]` to the model, so each test here hands a fake model to `_ReactAgent` and reads what
the model was actually given, or what the run actually did with what it returned.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.config import Settings
from felix.context import AuthContext, RequestContext, async_run_with_context
from felix.hooks import get_agent_hooks, reset_agent_hooks
from felix.manifests.schema import ModelSpec
from felix.patterns.react import _ReactAgent
from felix.patterns.types import ChatMessage, InvokeInput, ToolCall
from felix.tools.types import Tool, ToolInput, ToolInvocationCtx, ToolOutput
from felix_ai.types import ModelChatResult, StreamDelta, TokenUsage


@pytest.fixture(autouse=True)
def _clean_hooks():
    reset_agent_hooks()
    yield
    reset_agent_hooks()


class _Recorder:
    transport = "local"

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, args: ToolInput, ctx: ToolInvocationCtx | None = None) -> ToolOutput:
        self.calls += 1
        return "tool ran"


class _Model:
    """Asks for `echo` once, then answers; records the messages each call was sent."""

    model_id = "fake-model"

    def __init__(self, *, call_tool: bool = True) -> None:
        self.seen: list[list[ChatMessage]] = []
        self.call_tool = call_tool

    async def chat(self, messages: list[ChatMessage], tools: list, opts=None) -> ModelChatResult:
        self.seen.append(list(messages))
        if self.call_tool and len(self.seen) == 1:
            return ModelChatResult(
                message=ChatMessage(
                    role="assistant", content="", tool_calls=[ToolCall(id="c1", name="echo", args={})]
                ),
                stop_reason="tool_use",
                usage=TokenUsage(),
            )
        return ModelChatResult(
            message=ChatMessage(role="assistant", content="done"), stop_reason="end_turn", usage=TokenUsage()
        )


class _StreamingModel(_Model):
    async def stream_turn(self, messages: list[ChatMessage], tools: list, opts=None):
        result = await self.chat(messages, tools, opts)
        yield StreamDelta(kind="text", text=result.message.content or "")
        yield result


def _agent(model: Any, tool: _Recorder) -> _ReactAgent:
    agent = _ReactAgent(
        tools=[Tool(name="echo", description="d", args_schema=None, executor=tool)],
        pattern="react",
        manifest_id="hooked",
        manifest_version="1",
        system_prompt="s",
        model_spec=ModelSpec(id="fake-model"),
        settings=None,
        recursion_limit=5,
    )
    agent._resolve_model = lambda _input: model  # type: ignore[method-assign]
    return agent


def _ctx() -> RequestContext:
    settings = Settings(
        allow_insecure=True, auth_mode="none", environment="development", database_url="memory://model-hooks"
    )
    return RequestContext(
        settings=settings, auth=AuthContext(tenant_id="default", scopes=frozenset()), manifest_id="hooked"
    )


def _contents(messages: list[ChatMessage]) -> list[str]:
    return [str(m.content) for m in messages if m.role == "user"]


async def _invoke(agent: _ReactAgent, text: str = "secret question"):
    ctx = _ctx()
    async with async_run_with_context(ctx):
        return await agent.invoke(
            InvokeInput(messages=[ChatMessage(role="user", content=text)], tenant_id="default")
        )


@pytest.mark.asyncio
async def test_before_model_replaces_what_one_call_sends_and_leaves_history_alone() -> None:
    calls = 0

    def redact_first_call(request: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any] | None:
        nonlocal calls
        calls += 1
        if calls > 1:
            return None
        return {
            "messages": [
                ChatMessage(role="user", content="[redacted]") if m.role == "user" else m
                for m in request["messages"]
            ]
        }

    get_agent_hooks().register_before_model(redact_first_call)
    model = _Model()
    await _invoke(_agent(model, _Recorder()))

    assert len(model.seen) == 2
    assert _contents(model.seen[0]) == ["[redacted]"], "the first call was not sent the hook's messages"
    assert _contents(model.seen[1]) == ["secret question"], (
        "the second call saw the first call's replacement, so the hook rewrote the run's history"
    )


@pytest.mark.asyncio
async def test_before_model_hooks_chain_and_see_the_call_context() -> None:
    seen: list[tuple[list[str], dict[str, Any]]] = []

    def first(request, ctx):
        seen.append((list(request["tools"]), dict(ctx)))
        return {"messages": [*request["messages"], ChatMessage(role="user", content="from first")]}

    def second(request, ctx):
        assert _contents(request["messages"])[-1] == "from first"
        return {"messages": [*request["messages"], ChatMessage(role="user", content="from second")]}

    hooks = get_agent_hooks()
    hooks.register_before_model(first)
    hooks.register_before_model(second)
    model = _Model(call_tool=False)
    await _invoke(_agent(model, _Recorder()))

    assert _contents(model.seen[0])[-2:] == ["from first", "from second"]
    tools, ctx = seen[0]
    assert tools == ["echo"]
    assert ctx["manifest_id"] == "hooked"
    assert ctx["model_id"] == "fake-model"


@pytest.mark.asyncio
async def test_before_model_applies_to_a_streamed_call() -> None:
    get_agent_hooks().register_before_model(
        lambda request, ctx: {"messages": [ChatMessage(role="user", content="streamed via hook")]}
    )
    model = _StreamingModel(call_tool=False)
    agent = _agent(model, _Recorder())
    ctx = _ctx()
    async with async_run_with_context(ctx):
        _ = [
            ev
            async for ev in agent.stream_events(
                InvokeInput(messages=[ChatMessage(role="user", content="original")], tenant_id="default")
            )
        ]

    assert model.seen and _contents(model.seen[0]) == ["streamed via hook"]


@pytest.mark.asyncio
async def test_after_model_replacement_is_what_the_run_acts_on() -> None:
    """Dropping the tool call from the reply means the tool never runs and the reply is the hook's."""
    stop_reasons: list[str | None] = []

    def strip_tool_calls(response, ctx):
        stop_reasons.append(response["stop_reason"])
        if response["message"].tool_calls:
            return {"message": ChatMessage(role="assistant", content="not calling that")}
        return None

    get_agent_hooks().register_after_model(strip_tool_calls)
    tool = _Recorder()
    out = await _invoke(_agent(_Model(), tool))

    assert tool.calls == 0, "the run executed a tool call the after_model hook removed"
    assert out.final.content == "not calling that"
    assert stop_reasons == ["tool_use"]


@pytest.mark.asyncio
async def test_after_model_ignores_a_bad_replacement_and_a_raising_hook() -> None:
    def not_assistant(response, ctx):
        return {"message": ChatMessage(role="user", content="impersonating the user")}

    def raises(response, ctx):
        raise RuntimeError("hook bug")

    hooks = get_agent_hooks()
    hooks.register_after_model(not_assistant)
    hooks.register_after_model(raises)
    tool = _Recorder()
    out = await _invoke(_agent(_Model(), tool))

    assert tool.calls == 1
    assert out.final.content == "done"
