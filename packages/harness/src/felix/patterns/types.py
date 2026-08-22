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
    media_type: str = "image/png"
    filename: str | None = None
    detail: str | None = None  # openai: low|high|auto


@dataclass(slots=True)
class ContentBlock:
    """Typed content part (text or image) for multimodal messages."""

    type: Literal["text", "image_url", "image"]
    text: str | None = None
    url: str | None = None
    media_type: str | None = None
    detail: str | None = None


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
    content_blocks: list[ContentBlock] | None = None
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
                        tc.get("args") or tc.get("arguments") or tc.get("function", {}).get("arguments") or {}
                    ),
                )
                if isinstance(tc, dict)
                else tc
                for tc in tool_calls_raw
            ]

        content_raw = data.get("content")
        text_content = ""
        blocks: list[ContentBlock] | None = None
        attachments: list[ImageAttachment] | None = None

        if isinstance(content_raw, list):
            blocks = []
            parts: list[str] = []
            atts: list[ImageAttachment] = []
            for part in content_raw:
                if not isinstance(part, dict):
                    continue
                ptype = str(part.get("type") or "text")
                if ptype == "text":
                    t = str(part.get("text") or "")
                    parts.append(t)
                    blocks.append(ContentBlock(type="text", text=t))
                elif ptype in {"image_url", "image"}:
                    url_obj = part.get("image_url") if isinstance(part.get("image_url"), dict) else {}
                    url = str(
                        part.get("url") or url_obj.get("url") or part.get("source", {}).get("url") or ""
                    )
                    media = str(
                        part.get("media_type")
                        or part.get("mime_type")
                        or url_obj.get("media_type")
                        or "image/png"
                    )
                    detail = part.get("detail") or url_obj.get("detail")
                    blocks.append(
                        ContentBlock(
                            type="image_url",
                            url=url,
                            media_type=media,
                            detail=str(detail) if detail else None,
                        )
                    )
                    atts.append(
                        ImageAttachment(
                            url=url,
                            media_type=media,
                            filename=part.get("filename"),
                            detail=str(detail) if detail else None,
                        )
                    )
            text_content = "\n".join(p for p in parts if p)
            attachments = atts or None
            if not blocks:
                blocks = None
        else:
            text_content = str(content_raw or "")

        att_raw = data.get("attachments")
        if att_raw and attachments is None:
            attachments = [
                ImageAttachment(
                    url=str(a.get("url") or ""),
                    media_type=str(a.get("media_type") or a.get("mime_type") or "image/png"),
                    filename=a.get("filename"),
                    detail=a.get("detail"),
                )
                if isinstance(a, dict)
                else a
                for a in att_raw
            ]

        return cls(
            role=data.get("role") or "user",
            content=text_content,
            tool_call_id=data.get("tool_call_id"),
            name=data.get("name"),
            tool_calls=tool_calls,
            attachments=attachments,
            content_blocks=blocks or data.get("content_blocks"),
            thinking=data.get("thinking"),
        )

    def model_dump(self) -> dict[str, Any]:
        out: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_call_id is not None:
            out["tool_call_id"] = self.tool_call_id
        if self.name is not None:
            out["name"] = self.name
        if self.tool_calls is not None:
            out["tool_calls"] = [{"id": tc.id, "name": tc.name, "args": tc.args} for tc in self.tool_calls]
        if self.thinking is not None:
            out["thinking"] = self.thinking
        if self.attachments:
            out["attachments"] = [
                {
                    "url": a.url,
                    "media_type": a.media_type,
                    "filename": a.filename,
                    "detail": a.detail,
                }
                for a in self.attachments
            ]
        if self.content_blocks:
            out["content_blocks"] = [
                {
                    "type": b.type,
                    "text": b.text,
                    "url": b.url,
                    "media_type": b.media_type,
                    "detail": b.detail,
                }
                for b in self.content_blocks
            ]
        return out


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
