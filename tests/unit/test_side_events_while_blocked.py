"""A blocking tool's side events reach the stream while it is still blocked.

`approval_required`, `tool_request` and `ui_request` are all emitted from inside a
tool executor that then *blocks* waiting for the answer. They reach the stream
through a queue the react loop drains, and the loop used to drain it only after the
whole tool batch returned — so the frame asking for a decision was flushed in the
same beat as the `tool_end` that the decision produced.

Measured against a live harness before the fix: a stream blocked on a gated
`write_file` for 75 seconds carried no `approval_required` at all, and the client
showed a tool card sitting at `running` with nothing to say why.

The ordering is the whole contract, so it is asserted the only way that cannot pass
by luck: the tool is released *by* the arrival of its own frame. Under the old
drain-after-batch code the frame never arrives and this test times out rather than
failing on an assertion — which is exactly what the bug did to a client.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from felix.patterns.model import ModelChatResult, TokenUsage
from felix.patterns.react import _ReactAgent
from felix.patterns.types import ChatMessage, InvokeInput
from felix.side_events import emit as emit_side_event
from felix.tools.types import Tool, ToolInvocationCtx

THREAD = "default:side-events"


class _CallsOneTool:
    """Calls `gated` once, then answers."""

    model_id = "scripted"

    def __init__(self) -> None:
        self._served = False

    async def stream_turn(self, messages: list[ChatMessage], tools: list[Any], opts: Any = None):
        yield await self.chat(messages, tools, opts)

    async def chat(self, messages: list[ChatMessage], tools: list[Any], opts: Any = None):
        if self._served:
            return ModelChatResult(
                message=ChatMessage(role="assistant", content="done"),
                stop_reason="end_turn",
                usage=TokenUsage(),
            )
        self._served = True
        from felix_ai.types import ToolCall

        return ModelChatResult(
            message=ChatMessage(
                role="assistant",
                content="",
                tool_calls=[ToolCall(id="call_1", name="gated", args={"path": "notes.txt"})],
            ),
            stop_reason="tool_use",
            usage=TokenUsage(),
        )


class _BlockingExecutor:
    """Emits an `approval_required`, then waits to be released."""

    transport = "local"

    def __init__(self) -> None:
        self.released = asyncio.Event()

    async def execute(self, args: Any, ctx: ToolInvocationCtx | None = None) -> str:
        await emit_side_event(
            THREAD,
            "approval_required",
            {"approval_id": "appr_1", "tool_name": "gated", "args": dict(args), "rule_id": "r1"},
        )
        await self.released.wait()
        return "written"


def _agent(tool: Tool) -> _ReactAgent:
    agent = _ReactAgent(
        tools=[tool],
        pattern="react",
        manifest_id="test",
        manifest_version="1",
        system_prompt="s",
        model_spec=None,
        settings=None,
        recursion_limit=3,
    )
    agent._resolve_model = lambda _input: _CallsOneTool()  # type: ignore[method-assign]
    return agent


@pytest.mark.asyncio
async def test_approval_frame_arrives_before_the_tool_is_released() -> None:
    executor = _BlockingExecutor()
    tool = Tool(name="gated", description="gated", args_schema=None, executor=executor)
    agent = _agent(tool)

    seen: list[str] = []

    async def _run() -> None:
        stream = agent.stream_events(
            InvokeInput(messages=[ChatMessage(role="user", content="hi")], thread_id=THREAD)
        )
        async for event in stream:
            seen.append(event.event)
            # The release is the assertion. Reaching this line means the frame was
            # delivered while the tool was still waiting inside `run_batch`.
            if event.event == "approval_required":
                executor.released.set()

    await asyncio.wait_for(_run(), timeout=5)

    assert "approval_required" in seen, "the frame never reached the stream"
    assert seen.index("approval_required") < seen.index("tool_end"), (
        "the frame arrived with or after the result it was asking permission for"
    )
    assert executor.released.is_set()


@pytest.mark.asyncio
async def test_a_batch_that_never_blocks_still_delivers_its_side_events() -> None:
    """The fast path must not lose what the slow path gained."""

    class _Fast:
        transport = "local"

        async def execute(self, args: Any, ctx: ToolInvocationCtx | None = None) -> str:
            await emit_side_event(THREAD, "ui_request", {"request_id": "u1"})
            return "ok"

    agent = _agent(Tool(name="gated", description="g", args_schema=None, executor=_Fast()))
    events = [
        e.event
        async for e in agent.stream_events(
            InvokeInput(messages=[ChatMessage(role="user", content="hi")], thread_id=THREAD)
        )
    ]
    assert "ui_request" in events
