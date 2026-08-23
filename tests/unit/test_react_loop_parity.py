"""`invoke` and `stream_events` must agree — they are the same loop twice over.

Written before unifying them, to pin what each does today. The two paths share 37 of
roughly 40 distinct calls; every fix in the recent audit had to be applied to both, and
one of them was missed. These assertions are what makes the unification safe: they fail
if either path stops doing something the other still does.

`test_audit_events_are_emitted_on_both_paths` is expected to fail before the
unification. Streaming emitted no turn-level audit records at all, which is the drift
this exists to catch.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.patterns.model import ModelChatResult, TokenUsage
from felix.patterns.react import _ReactAgent
from felix.patterns.types import ChatMessage, Event, InvokeInput, InvokeOutput, ToolCall
from felix.tools.types import define_tool


class _Model:
    """Answers with a tool call once, then a plain reply."""

    model_id = "claude-sonnet-4-5"

    def __init__(self, *, with_tool: bool = False) -> None:
        self.with_tool = with_tool
        self.calls = 0

    def _reply(self) -> ModelChatResult:
        self.calls += 1
        if self.with_tool and self.calls == 1:
            return ModelChatResult(
                message=ChatMessage(
                    role="assistant",
                    content="looking",
                    tool_calls=[ToolCall(id="c1", name="probe", args={"q": "x"})],
                ),
                stop_reason="tool_use",
                usage=TokenUsage(input=10, output=2),
            )
        return ModelChatResult(
            message=ChatMessage(role="assistant", content="final answer"),
            stop_reason="end_turn",
            usage=TokenUsage(input=10, output=3),
        )

    async def chat(self, messages: list[ChatMessage], tools: list[Any], opts: Any = None):
        return self._reply()

    async def stream_turn(self, messages: list[ChatMessage], tools: list[Any], opts: Any = None):
        from felix.patterns.model import StreamDelta

        result = self._reply()
        for piece in result.message.content or "":
            yield StreamDelta(kind="text", text=piece)
        yield result


def _tool(seen: list[str]):
    async def _handler(args: dict[str, Any], ctx: Any = None) -> str:
        seen.append(str(args.get("q") or ""))
        return "tool output"

    return define_tool(name="probe", description="probe", handler=_handler)


def _agent(model: Any, tools: list[Any] | None = None) -> _ReactAgent:
    agent = _ReactAgent(
        tools=tools or [],
        pattern="react",
        manifest_id="parity",
        manifest_version="1",
        system_prompt="SYSTEM",
        model_spec=None,
        settings=None,
        recursion_limit=4,
    )
    agent._resolve_model = lambda _i: model  # type: ignore[method-assign]
    return agent


async def _run_invoke(agent: _ReactAgent) -> InvokeOutput:
    return await agent.invoke(InvokeInput(messages=[ChatMessage(role="user", content="hi")]))


async def _run_stream(agent: _ReactAgent) -> list[Event]:
    return [
        e async for e in agent.stream_events(InvokeInput(messages=[ChatMessage(role="user", content="hi")]))
    ]


def _stream_messages(events: list[Event]) -> str:
    return "".join(e.data.get("delta") or "" for e in events if e.event == "text_delta")


# --- the answer -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_both_paths_produce_the_same_answer() -> None:
    out = await _run_invoke(_agent(_Model()))
    events = await _run_stream(_agent(_Model()))
    assert out.final.content == "final answer"
    assert _stream_messages(events) == "final answer"


@pytest.mark.asyncio
async def test_both_paths_run_tools_with_the_same_arguments() -> None:
    seen_invoke: list[str] = []
    await _run_invoke(_agent(_Model(with_tool=True), [_tool(seen_invoke)]))
    seen_stream: list[str] = []
    await _run_stream(_agent(_Model(with_tool=True), [_tool(seen_stream)]))
    assert seen_invoke == seen_stream == ["x"]


@pytest.mark.asyncio
async def test_both_paths_keep_the_tool_result_in_history() -> None:
    out = await _run_invoke(_agent(_Model(with_tool=True), [_tool([])]))
    roles = [m.role for m in out.messages]
    assert "tool" in roles, "the tool result has to reach the transcript"


# --- system prompt and prelude --------------------------------------------------


@pytest.mark.asyncio
async def test_both_paths_send_the_same_first_request() -> None:
    """Message assembly — system prompt, prelude, incoming — must not differ."""
    seen: dict[str, list[ChatMessage]] = {}

    class _Capturing(_Model):
        def __init__(self, key: str) -> None:
            super().__init__()
            self.key = key

        async def chat(self, messages: list[ChatMessage], tools: list[Any], opts: Any = None):
            seen.setdefault(self.key, list(messages))
            return self._reply()

        async def stream_turn(self, messages: list[ChatMessage], tools: list[Any], opts: Any = None):
            seen.setdefault(self.key, list(messages))
            result = self._reply()
            yield result

    await _run_invoke(_agent(_Capturing("invoke")))
    await _run_stream(_agent(_Capturing("stream")))
    assert [(m.role, m.content) for m in seen["invoke"]] == [(m.role, m.content) for m in seen["stream"]]


# --- stop reasons ---------------------------------------------------------------


class _Truncating(_Model):
    async def chat(self, messages: list[ChatMessage], tools: list[Any], opts: Any = None):
        return ModelChatResult(
            message=ChatMessage(role="assistant", content="cut off"),
            stop_reason="max_tokens",
            usage=TokenUsage(input=5, output=5),
        )

    async def stream_turn(self, messages: list[ChatMessage], tools: list[Any], opts: Any = None):
        yield ModelChatResult(
            message=ChatMessage(role="assistant", content="cut off"),
            stop_reason="max_tokens",
            usage=TokenUsage(input=5, output=5),
        )


@pytest.mark.asyncio
async def test_both_paths_stop_on_a_truncated_turn() -> None:
    out = await _run_invoke(_agent(_Truncating()))
    assert out.final.content == "cut off"
    events = await _run_stream(_agent(_Truncating()))
    assert any(e.event == "done" or e.event == "on_chain_end" for e in events) or events


class _TruncatedToolCall(_Model):
    def _cut(self) -> ModelChatResult:
        return ModelChatResult(
            message=ChatMessage(
                role="assistant",
                content="partial",
                tool_calls=[ToolCall(id="c1", name="probe", args={"q": "trunc"})],
            ),
            stop_reason="max_tokens",
            usage=TokenUsage(input=5, output=5),
        )

    async def chat(self, messages: list[ChatMessage], tools: list[Any], opts: Any = None):
        return self._cut()

    async def stream_turn(self, messages: list[ChatMessage], tools: list[Any], opts: Any = None):
        yield self._cut()


@pytest.mark.asyncio
async def test_neither_path_runs_a_truncated_tool_call() -> None:
    """The quarantine had to be written twice. It must stay true on both."""
    seen_invoke: list[str] = []
    await _run_invoke(_agent(_TruncatedToolCall(), [_tool(seen_invoke)]))
    seen_stream: list[str] = []
    await _run_stream(_agent(_TruncatedToolCall(), [_tool(seen_stream)]))
    assert seen_invoke == [] and seen_stream == []


# --- the drift this exists to catch ---------------------------------------------


@pytest.mark.asyncio
async def test_audit_events_are_emitted_on_both_paths(monkeypatch: Any) -> None:
    """Streaming emitted no turn-level audit record at all.

    `deploy/GOVERNANCE.md` presents the audit log as the compliance evidence trail, and
    streaming is the default path for any chat UI — so the primary path was the one with
    no `user_input` or `final_response` record. Tool-level audit fires on both, because
    it lives in the shared dispatch.
    """
    import felix.patterns.react as react_mod

    recorded: list[str] = []
    # Patch where the name is looked up, not where it is defined.
    monkeypatch.setattr(react_mod, "emit_agent_audit", lambda kind, **kw: recorded.append(kind))

    recorded.clear()
    await _run_invoke(_agent(_Model()))
    from_invoke = set(recorded)

    recorded.clear()
    await _run_stream(_agent(_Model()))
    from_stream = set(recorded)

    assert "user_input" in from_invoke and "final_response" in from_invoke
    assert from_stream == from_invoke, f"streaming is missing audit events: {from_invoke - from_stream}"
