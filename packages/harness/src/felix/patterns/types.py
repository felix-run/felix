"""Pattern runtime types — Agent invoke / stream contract."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from felix.tools.types import Tool

Role = Literal["user", "assistant", "system", "tool"]


@dataclass(slots=True)
class ImageAttachment:
    url: str
    media_type: str
    filename: str | None = None


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]


@dataclass(slots=True)
class ChatMessage:
    role: Role
    content: str
    tool_call_id: str | None = None
    name: str | None = None
    tool_calls: list[ToolCall] | None = None
    attachments: list[ImageAttachment] | None = None
    thinking: list[dict[str, Any]] | None = None

    @classmethod
    def model_validate(cls, data: Any) -> ChatMessage:
        if isinstance(data, cls):
            return data
        if not isinstance(data, dict):
            raise TypeError(f"ChatMessage expects mapping, got {type(data)!r}")
        tool_calls_raw = data.get("tool_calls")
        tool_calls: list[ToolCall] | None = None
        if tool_calls_raw:
            tool_calls = [
                ToolCall(
                    id=str(tc.get("id") or ""),
                    name=str(tc.get("name") or tc.get("function", {}).get("name") or ""),
                    args=dict(
                        tc.get("args")
                        or tc.get("arguments")
                        or tc.get("function", {}).get("arguments")
                        or {}
                    ),
                )
                if isinstance(tc, dict)
                else tc
                for tc in tool_calls_raw
            ]
        return cls(
            role=data.get("role") or "user",
            content=str(data.get("content") or ""),
            tool_call_id=data.get("tool_call_id"),
            name=data.get("name"),
            tool_calls=tool_calls,
            thinking=data.get("thinking"),
        )

    def model_dump(self) -> dict[str, Any]:
        out: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_call_id is not None:
            out["tool_call_id"] = self.tool_call_id
        if self.name is not None:
            out["name"] = self.name
        if self.tool_calls is not None:
            out["tool_calls"] = [
                {"id": tc.id, "name": tc.name, "args": tc.args} for tc in self.tool_calls
            ]
        if self.thinking is not None:
            out["thinking"] = self.thinking
        return out


@dataclass(slots=True)
class InvokeInput:
    messages: list[ChatMessage]
    thread_id: str | None = None
    model_id: str | None = None
    tenant_id: str | None = None


@dataclass(slots=True)
class InvokeOutput:
    messages: list[ChatMessage]
    final: ChatMessage


# Back-compat alias used by TS port call sites.
InvokeResult = InvokeOutput


@dataclass(slots=True)
class Event:
    event: str
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def type(self) -> str:
        return self.event

    @property
    def text(self) -> str:
        chunk = self.data.get("chunk")
        if isinstance(chunk, dict):
            return str(chunk.get("content") or "")
        return str(self.data.get("text") or self.data.get("delta") or "")

    def model_dump(self) -> dict[str, Any]:
        return {"event": self.event, "type": self.event, "data": self.data, "text": self.text}


StreamEvent = Event


@runtime_checkable
class Agent(Protocol):
    tools: list[Tool] | tuple[Tool, ...]
    pattern: str
    manifest_id: str
    manifest_version: str

    async def invoke(self, input: InvokeInput) -> InvokeOutput: ...

    def stream_events(self, input: InvokeInput) -> AsyncIterator[Event]: ...


__all__ = [
    "Agent",
    "ChatMessage",
    "Event",
    "ImageAttachment",
    "InvokeInput",
    "InvokeOutput",
    "InvokeResult",
    "Role",
    "StreamEvent",
    "ToolCall",
]
