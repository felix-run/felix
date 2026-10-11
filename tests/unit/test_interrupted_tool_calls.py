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


# --- a run that dies mid-batch leaves the call in the log ------------------------
#
# felix-run/felix#531. The assistant message holding a batch's tool calls was appended only
# once the whole batch returned, so a run that died inside a tool -- a worker restart, a lost
# fiber lease -- left no trace of a call that may already have taken effect. The re-run asked
# the model again from the user's turn, and it issued the same writes again: on a production
# `cowork` thread, the same files were written twice. Written ahead of the batch, the call is
# in the history, and the next run closes it as interrupted instead.


class _CallsChargeOnce:
    """Asks for `charge` once; answers in text after that."""

    model_id = "claude-sonnet-4-5"

    def __init__(self) -> None:
        self.calls = 0
        self.seen: list[ChatMessage] = []

    async def chat(self, messages: list[ChatMessage], tools: list[Any], opts: Any = None):
        self.calls += 1
        self.seen = list(messages)
        if self.calls == 1:
            return ModelChatResult(
                message=_assistant_calling("charge", "call_pay"),
                stop_reason="tool_use",
                usage=TokenUsage(input=5, output=5),
            )
        return ModelChatResult(
            message=ChatMessage(role="assistant", content="checked; it went through"),
            stop_reason="end_turn",
            usage=TokenUsage(input=5, output=5),
        )


async def test_a_run_that_dies_inside_a_tool_leaves_the_call_for_the_next_run_to_close() -> None:
    import asyncio

    from felix.config import Settings
    from felix.session.store import get_session_store
    from felix.session.types import event_to_chat_message

    settings = Settings(database_url="memory://interrupted-mid-batch", object_store="memory", redis_url="")
    store = get_session_store(settings, tenant_id="default")
    thread = "default:dies-mid-batch"
    entered = asyncio.Event()
    charged = 0

    async def _charge(args: dict[str, Any], ctx: Any = None) -> str:
        nonlocal charged
        charged += 1
        entered.set()
        await asyncio.Event().wait()  # the worker dies here, mid-call
        return "charged"

    charge = define_tool(name="charge", description="takes money", handler=_charge)
    model = _CallsChargeOnce()
    agent = _ReactAgent(
        tools=[charge],
        pattern="react",
        manifest_id="test",
        manifest_version="1",
        system_prompt="s",
        model_spec=None,
        settings=None,
        recursion_limit=3,
        session_store=store,
    )
    agent._resolve_model = lambda _i: model  # type: ignore[method-assign]

    run = asyncio.create_task(
        agent.invoke(InvokeInput(messages=[ChatMessage(role="user", content="pay it")], thread_id=thread))
    )
    await asyncio.wait_for(entered.wait(), timeout=5)
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run

    logged = [event_to_chat_message(e) for e in await store.open(thread).get_events()]
    calls = [c.id for m in logged if m.role == "assistant" for c in (m.tool_calls or [])]
    assert calls == ["call_pay"], f"the call that was running is not in the log: {logged}"

    # The next run, from the log: the model is told the call did not finish and may already
    # have taken effect -- not handed a history in which it never happened.
    await agent.invoke(InvokeInput(messages=[*logged, ChatMessage(role="user", content="[continue]")]))
    closed = [m for m in model.seen if m.role == "tool" and m.tool_call_id == "call_pay"]
    assert closed and "may have already taken effect" in closed[0].content.lower()
    assert charged == 1


async def test_a_resumed_run_withdraws_the_gates_its_interrupted_calls_left_open() -> None:
    """felix-run/felix#531: the dead attempt's approval and client request stop being offered.

    Left open, the approval stayed `pending` to its deadline on every surface that lists one,
    and approving it then installed a grant for a call the model had been told did not finish.
    """
    from felix.approvals import store as approvals
    from felix.config import Settings
    from felix.tools import client_requests

    settings = Settings(database_url="memory://interrupted-gates", object_store="memory", redis_url="")
    thread = "default:gated-and-gone"
    gate = await approvals.create_pending(
        settings,
        "default",
        tool_name="charge",
        call_signature="sig-pay",
        manifest_id="test",
        args={},
        thread_id=thread,
        tool_call_id="call_pay",
    )
    await client_requests.record(thread, {"id": "call_pay", "name": "charge"}, timeout=300)

    model = _Capturing()
    agent = _ReactAgent(
        tools=[UNSAFE],
        pattern="react",
        manifest_id="test",
        manifest_version="1",
        system_prompt="s",
        model_spec=None,
        settings=settings,
        recursion_limit=3,
    )
    agent._resolve_model = lambda _i: model  # type: ignore[method-assign]
    await agent.invoke(
        InvokeInput(
            messages=[
                _assistant_calling("charge", "call_pay"),
                ChatMessage(role="user", content="[continue]"),
            ],
            thread_id=thread,
        )
    )

    row = await approvals.get_approval(settings, "default", gate["id"])
    assert row is not None and (row["status"], row["decision_note"]) == ("denied", "interrupted")
    assert await client_requests.pending(thread) == []
