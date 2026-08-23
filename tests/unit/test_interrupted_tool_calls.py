"""A run that died mid-tool left a thread that could not be resumed.

The provider requires every tool call in the history to be answered. A run killed while
a tool was in flight leaves an assistant turn holding a call with no result, so resuming
that thread sent a transcript the provider rejects outright — the one situation
`/chat/continue` exists for was the one it could not handle.

Whether the effect actually happened is not knowable after the fact, which is what
`Tool.replay_safe` is for: re-running a search costs latency, re-running a payment
charges twice, so the default is that a tool must not be replayed.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.patterns.model import ModelChatResult, TokenUsage
from felix.patterns.react import _interrupted_tool_results, _ReactAgent
from felix.patterns.types import ChatMessage, InvokeInput, ToolCall
from felix.tools.types import define_tool


async def _noop(args: dict[str, Any], ctx: Any = None) -> str:
    return "ok"


SAFE = define_tool(name="search", description="read only", handler=_noop, replay_safe=True)
UNSAFE = define_tool(name="charge", description="takes money", handler=_noop)
TOOLS = {t.name: t for t in (SAFE, UNSAFE)}


def _assistant_calling(name: str, call_id: str = "c1") -> ChatMessage:
    return ChatMessage(
        role="assistant",
        content="calling",
        tool_calls=[ToolCall(id=call_id, name=name, args={})],
    )


# --- what gets closed out -------------------------------------------------------


def test_an_unanswered_call_gets_a_result() -> None:
    results = _interrupted_tool_results([_assistant_calling("search")], TOOLS)
    assert len(results) == 1
    assert results[0].role == "tool"
    assert results[0].tool_call_id == "c1"
    assert "[error/interrupted]" in results[0].content


def test_an_answered_call_is_left_alone() -> None:
    messages = [
        _assistant_calling("search"),
        ChatMessage(role="tool", tool_call_id="c1", name="search", content="found it"),
    ]
    assert _interrupted_tool_results(messages, TOOLS) == []


def test_only_the_unanswered_call_of_a_batch_is_closed() -> None:
    messages = [
        ChatMessage(
            role="assistant",
            content="calling both",
            tool_calls=[
                ToolCall(id="c1", name="search", args={}),
                ToolCall(id="c2", name="charge", args={}),
            ],
        ),
        ChatMessage(role="tool", tool_call_id="c1", name="search", content="done"),
    ]
    results = _interrupted_tool_results(messages, TOOLS)
    assert [r.tool_call_id for r in results] == ["c2"]


def test_a_clean_transcript_produces_nothing() -> None:
    messages = [
        ChatMessage(role="user", content="hi"),
        ChatMessage(role="assistant", content="answer"),
    ]
    assert _interrupted_tool_results(messages, TOOLS) == []


def test_calls_across_several_turns_are_all_closed() -> None:
    messages = [
        _assistant_calling("search", "c1"),
        ChatMessage(role="user", content="still there?"),
        _assistant_calling("charge", "c2"),
    ]
    assert [r.tool_call_id for r in _interrupted_tool_results(messages, TOOLS)] == ["c1", "c2"]


# --- what the model is told -----------------------------------------------------


def test_a_replay_safe_tool_is_marked_retryable() -> None:
    (result,) = _interrupted_tool_results([_assistant_calling("search")], TOOLS)
    assert "safe to call again" in result.content.lower()


def test_an_effectful_tool_is_not_marked_retryable() -> None:
    """The default. Re-running a charge is worse than not finishing it."""
    (result,) = _interrupted_tool_results([_assistant_calling("charge")], TOOLS)
    assert "safe to call again" not in result.content.lower()
    assert "may have already taken effect" in result.content.lower()


def test_an_unknown_tool_is_treated_as_unsafe() -> None:
    """A tool no longer in the manifest cannot be reasoned about, so assume the worst."""
    (result,) = _interrupted_tool_results([_assistant_calling("vanished")], TOOLS)
    assert "safe to call again" not in result.content.lower()


def test_replay_safe_defaults_to_false() -> None:
    assert define_tool(name="x", description="d", handler=_noop).replay_safe is False


# --- the resumed request --------------------------------------------------------


class _Capturing:
    model_id = "claude-sonnet-4-5"

    def __init__(self) -> None:
        self.seen: list[ChatMessage] = []

    async def chat(self, messages: list[ChatMessage], tools: list[Any], opts: Any = None):
        self.seen = list(messages)
        return ModelChatResult(
            message=ChatMessage(role="assistant", content="resumed"),
            stop_reason="end_turn",
            usage=TokenUsage(input=5, output=5),
        )


@pytest.mark.asyncio
async def test_a_resumed_run_answers_every_outstanding_call() -> None:
    """Without this the provider rejects the request for an unanswered tool call."""
    model = _Capturing()
    agent = _ReactAgent(
        tools=[SAFE, UNSAFE],
        pattern="react",
        manifest_id="test",
        manifest_version="1",
        system_prompt="s",
        model_spec=None,
        settings=None,
        recursion_limit=3,
    )
    agent._resolve_model = lambda _i: model  # type: ignore[method-assign]

    await agent.invoke(
        InvokeInput(
            messages=[
                ChatMessage(role="user", content="charge the card"),
                _assistant_calling("charge"),
                ChatMessage(role="user", content="[continue]"),
            ]
        )
    )

    called = {c.id for m in model.seen if m.role == "assistant" for c in (m.tool_calls or [])}
    answered = {m.tool_call_id for m in model.seen if m.role == "tool"}
    assert called and called <= answered, f"unanswered tool calls reached the provider: {called - answered}"
