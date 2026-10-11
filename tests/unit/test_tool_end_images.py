"""A tool's stored images ride on its `tool_end` frame.

A screenshot used to reach a watching client only on the next read of the session log, so a
browser call's card read as a line of text for the whole run. The frame now carries each
stored image as a `felix-file://` reference; an inline image -- kept only when there was no
request tenant to store it under -- stays in the log, where its bytes already are.
"""

from __future__ import annotations

import base64
from typing import Any

from felix.patterns.model import ModelChatResult, TokenUsage
from felix.patterns.react import _ReactAgent, _tool_end_data
from felix.patterns.types import ChatMessage, InvokeInput
from felix.tools.types import ToolOutputDict, define_tool
from felix_ai.types import ImageAttachment, ToolCall

THREAD = "default:tool-end-images"
REF = "felix-file://0123456789abcdef0123456789abcdef"
INLINE = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\0" * 16).decode()


class _CallsSnapOnce:
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
        return ModelChatResult(
            message=ChatMessage(
                role="assistant", content="", tool_calls=[ToolCall(id="c1", name="snap", args={})]
            ),
            stop_reason="tool_use",
            usage=TokenUsage(),
        )


async def test_a_stored_image_reaches_the_stream_as_a_reference() -> None:
    async def handler(args: dict[str, Any]) -> Any:
        # As a tool that stored its own bytes through `store_image_bytes` returns them.
        return ToolOutputDict(
            content="a snapshot", attachments=[ImageAttachment(url=REF, media_type="image/png")]
        )

    tool = define_tool(
        name="snap", description="d", handler=handler, args_schema={"type": "object"}, transport="local"
    )
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
    agent._resolve_model = lambda _input: _CallsSnapOnce()  # type: ignore[method-assign]

    frames = [
        e.data
        async for e in agent.stream_events(
            InvokeInput(messages=[ChatMessage(role="user", content="hi")], thread_id=THREAD)
        )
        if e.event == "tool_end"
    ]
    assert len(frames) == 1
    assert frames[0]["output"] == "a snapshot"
    assert frames[0]["attachments"] == [{"url": REF, "media_type": "image/png"}]


def test_an_inline_image_stays_off_the_frame() -> None:
    msg = ChatMessage(
        role="tool",
        name="snap",
        tool_call_id="c1",
        content="x",
        attachments=[
            ImageAttachment(url=INLINE, media_type="image/png"),
            ImageAttachment(url=REF, media_type="image/jpeg"),
        ],
    )
    assert _tool_end_data(msg)["attachments"] == [{"url": REF, "media_type": "image/jpeg"}]


def test_a_result_with_no_images_keeps_its_old_shape() -> None:
    msg = ChatMessage(role="tool", name="read_file", tool_call_id="c2", content="text")
    assert _tool_end_data(msg) == {"name": "read_file", "output": "text", "id": "c2"}
    only_inline = ChatMessage(
        role="tool", name="snap", tool_call_id="c3", content="x", attachments=[ImageAttachment(url=INLINE)]
    )
    assert "attachments" not in _tool_end_data(only_inline)
