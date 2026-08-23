"""A turn cut off at max_tokens must not execute the tool calls it was writing.

The loop only inspected `stop_reason` on the branch where the assistant produced *no*
tool calls, so a response truncated mid-`tool_use` went straight to execution. The
arguments of such a call can be cut short and still parse — `{"path": "/srv/app/tmp"}`
truncated to `{"path": "/srv"}` is valid JSON naming a different target — and governance
cannot catch it, because command screening inspects the arguments it is handed.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.patterns.model import ModelChatResult, TokenUsage
from felix.patterns.react import _ReactAgent
from felix.patterns.types import ChatMessage, InvokeInput, ToolCall
from felix.tools.types import define_tool


class _ScriptedModel:
    """Returns one canned result, then a plain answer if the loop keeps going."""

    model_id = "scripted"

    def __init__(self, first: ModelChatResult) -> None:
        self._results = [first]
        self.calls = 0

    async def chat(self, messages: list[ChatMessage], tools: list[Any], opts: Any = None):
        self.calls += 1
        if self._results:
            return self._results.pop(0)
        return ModelChatResult(
            message=ChatMessage(role="assistant", content="done"),
            stop_reason="end_turn",
            usage=TokenUsage(),
        )


def _agent(model: _ScriptedModel, executed: list[str]) -> _ReactAgent:
    async def _handler(args: dict[str, Any], ctx: Any = None) -> str:
        executed.append(str(args.get("path") or ""))
        return "ok"

    agent = _ReactAgent(
        tools=[define_tool(name="rm", description="remove a path", handler=_handler)],
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


def _truncated_with_tool_call() -> ModelChatResult:
    return ModelChatResult(
        message=ChatMessage(
            role="assistant",
            content="removing the scratch dir",
            tool_calls=[ToolCall(id="c1", name="rm", args={"path": "/srv"})],
        ),
        stop_reason="max_tokens",
        usage=TokenUsage(input=10, output=10),
    )


@pytest.mark.asyncio
async def test_truncated_tool_call_is_not_executed() -> None:
    executed: list[str] = []
    model = _ScriptedModel(_truncated_with_tool_call())
    out = await _agent(model, executed).invoke(
        InvokeInput(messages=[ChatMessage(role="user", content="clean up")])
    )

    assert executed == [], "a tool call from a truncated message must never run"
    tool_msgs = [m for m in out.messages if m.role == "tool"]
    assert len(tool_msgs) == 1, "the call still needs exactly one result"
    assert tool_msgs[0].tool_call_id == "c1"
    assert "[error/truncated]" in tool_msgs[0].content
    assert model.calls == 1, "the turn ends; it does not loop on the truncated batch"


@pytest.mark.asyncio
async def test_untruncated_tool_call_still_runs() -> None:
    """The guard keys on stop_reason, so a normal tool-using turn is unaffected."""
    executed: list[str] = []
    result = _truncated_with_tool_call()
    result.stop_reason = "tool_use"
    out = await _agent(_ScriptedModel(result), executed).invoke(
        InvokeInput(messages=[ChatMessage(role="user", content="clean up")])
    )

    assert executed == ["/srv"]
    assert any("[error/truncated]" not in (m.content or "") for m in out.messages)


@pytest.mark.asyncio
async def test_truncated_without_tool_calls_is_unchanged() -> None:
    """Prose truncated at max_tokens keeps its existing behaviour: end, no tool result."""
    model = _ScriptedModel(
        ModelChatResult(
            message=ChatMessage(role="assistant", content="a partial answer"),
            stop_reason="max_tokens",
            usage=TokenUsage(),
        )
    )
    out = await _agent(model, []).invoke(InvokeInput(messages=[ChatMessage(role="user", content="explain")]))
    assert [m for m in out.messages if m.role == "tool"] == []
    assert out.final.content == "a partial answer"
