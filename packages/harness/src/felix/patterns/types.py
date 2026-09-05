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
    ModelChatOptions,
    Role,
    StopReason,
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
    # Per-request sampling, for callers whose wire carries it (`/v1`). `None` keeps the
    # manifest's `spec.model` values. A caller may only *lower* `max_tokens`: the react
    # loop clamps it to the manifest's ceiling before the model sees it.
    model_options: ModelChatOptions | None = None


@dataclass(slots=True)
class InvokeOutput:
    messages: list[ChatMessage]
    final: ChatMessage
    # Why the last model turn ended. A caller on the OpenAI wire maps it to
    # `finish_reason`; before this every reply said `stop`. The react loop and the reply
    # controls set it (`refusal` when governance replaced the reply); the delegating
    # patterns synthesise their final answer and report the default.
    stop_reason: StopReason = "end_turn"


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


def copy_agent_surface(wrapper: Any, inner: Any, *, manifest_id: str = "") -> None:
    """Give a wrapping agent the attributes the harness reads off an `Agent`.

    Every agent-level wrapper (inbound screening, the reply controls) has to look like
    the agent it wraps to `mcp/server.py`, the builder's sub-agent binding and the
    routes. One place to add the next attribute, instead of one per wrapper.
    """
    wrapper.tools = getattr(inner, "tools", [])
    wrapper.pattern = getattr(inner, "pattern", "")
    wrapper.manifest_id = getattr(inner, "manifest_id", manifest_id)
    wrapper.manifest_version = getattr(inner, "manifest_version", "")
    wrapper.system_prompt = getattr(inner, "system_prompt", "")


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
    "copy_agent_surface",
]
