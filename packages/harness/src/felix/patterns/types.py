"""Pattern runtime types — Agent invoke / stream contract.

The message types (`ChatMessage`, `ToolCall`, `ContentBlock`, `ImageAttachment`, `Role`)
moved to `felix_ai.types`: they describe a model turn, not a pattern, and the model layer
cannot import the harness. They are re-exported here because ~25 modules import them from
this path.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from felix_ai.types import (
    ChatMessage,
    ContentBlock,
    ImageAttachment,
    Role,
    ToolCall,
)

from felix.tools.types import Tool


@dataclass(slots=True)
class InvokeInput:
    messages: list[ChatMessage]
    thread_id: str | None = None
    model_id: str | None = None
    tenant_id: str | None = None
    thinking_level: str | None = None


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
    "ContentBlock",
    "Event",
    "ImageAttachment",
    "InvokeInput",
    "InvokeOutput",
    "InvokeResult",
    "Role",
    "StreamEvent",
    "ToolCall",
]
