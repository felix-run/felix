"""Stop during a streamed reply ends the run with what was shown, and asks the model nothing more."""

from __future__ import annotations

from typing import Any

from felix import steer
from felix.patterns.model import ModelChatResult, StreamDelta, TokenUsage
from felix.patterns.react import _ReactAgent
from felix.patterns.types import ChatMessage, InvokeInput, ToolCall
from felix.tools.types import define_tool

TENANT, THREAD = "default", "default:t-abort-mid-stream"


class _StoppedMidReply:
    """Streams two deltas, is stopped, and would answer a second request with a tool call."""

    model_id = "scripted"

    def __init__(self) -> None:
        self.requests: list[str] = []

    async def stream_turn(self, messages: list[ChatMessage], tools: list[Any], opts: Any = None):
        self.requests.append("stream_turn")
        yield StreamDelta(kind="text", text="The answer ")
        yield StreamDelta(kind="text", text="is ")
        await steer.request_abort(TENANT, THREAD)
        yield StreamDelta(kind="text", text="never shown")
        yield self._second_answer()

    async def chat(self, messages: list[ChatMessage], tools: list[Any], opts: Any = None) -> ModelChatResult:
        self.requests.append("chat")
        return self._second_answer()

    @staticmethod
    def _second_answer() -> ModelChatResult:
        call = ToolCall(id="c1", name="send", args={})
        return ModelChatResult(
            message=ChatMessage(role="assistant", content="a different answer", tool_calls=[call]),
            stop_reason="tool_use",
            usage=TokenUsage(),
        )


async def test_a_stop_mid_stream_makes_no_second_request_and_runs_no_tool() -> None:
    """The stream ended without a result, and the loop's fallback for stream-only providers
    took that as its cue to ask again in full: a second billed answer, stored as complete,
    whose tool calls ran after the person had pressed Stop."""
    ran: list[str] = []

    async def _send(args: dict[str, Any], ctx: Any = None) -> str:
        ran.append("send")
        return "sent"

    model = _StoppedMidReply()
    agent = _ReactAgent(
        tools=[define_tool(name="send", description="send it", handler=_send)],
        pattern="react",
        manifest_id="test",
        manifest_version="1",
        system_prompt="s",
        model_spec=None,
        settings=None,
        recursion_limit=3,
    )
    agent._resolve_model = lambda _input: model  # type: ignore[method-assign]

    try:
        events = [
            e
            async for e in agent.stream_events(
                InvokeInput(messages=[ChatMessage(role="user", content="hi")], thread_id=THREAD)
            )
        ]
    finally:
        await steer.release_run_queue(TENANT, THREAD)

    assert model.requests == ["stream_turn"]
    assert ran == []
    assert "".join(e.data["delta"] for e in events if e.event == "text_delta") == "The answer is "
    assert [e.event for e in events].count("aborted") == 1
