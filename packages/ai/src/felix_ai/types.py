"""Wire-neutral types for the model layer.

These moved out of `felix.patterns.types` and `felix.patterns.model` so the model layer
carries no dependency on the harness. `felix` re-exports every name from here, so the old
import paths keep working.

`ToolSchema` and `ModelConfig` are the two seams that replace what used to be direct
imports of `felix.tools.types.Tool` and `felix.config.Settings`. Both are Protocols that
the harness types satisfy structurally, so nothing on either side had to change shape.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol, TypeIs, runtime_checkable

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


@runtime_checkable
class ToolSchema(Protocol):
    """What a wire format needs of a tool: a name, a description, and a JSON schema.

    The harness `Tool` carries an executor, a transport, governance flags and replay
    safety — none of which any wire format reads. Narrowing to this Protocol is what lets
    the model layer stop importing `felix.tools.types`, and `Tool` satisfies it
    structurally, so no call site changed.
    """

    name: str
    description: str
    args_schema: dict[str, Any] | type[Any] | None
    raw_input_schema: dict[str, Any] | None


@runtime_checkable
class ModelConfig(Protocol):
    """The only setting a wire client reads off the harness `Settings` object.

    Read from the instance rather than a process-global, because `build_model` exists so a
    caller can pass its own configuration and every other field here honours that.
    """

    model_timeout_seconds: float


# `refusal` and `pause_turn` are real API outcomes. They were absent here, and the
# providers' stop_reason was never read at all — it was synthesised from whether the
# turn contained tool calls — so a truncated answer, a safety refusal, and a paused
# server-tool turn all presented to the agent loop as a normal completion.
StopReason = Literal[
    "end_turn",
    "tool_use",
    "max_tokens",
    "stop_sequence",
    "pause_turn",
    "refusal",
    "unknown",
]


@dataclass(slots=True)
class ModelRoute:
    provider: str
    model: str


@dataclass(slots=True)
class TokenUsage:
    input: int = 0
    output: int = 0
    cache_creation: int = 0
    cache_read: int = 0


@dataclass(slots=True)
class ModelChatOptions:
    temperature: float | None = None
    max_tokens: int | None = None
    signal: Any | None = None
    # Keep this request out of the conversation's prompt cache.
    #
    # A one-off request made during a run — summarising for compaction, scoring a judge —
    # shares the thread's cache key by default and carries a completely different prefix.
    # On an OpenAI-style endpoint that churns the conversation's cached prefix, so the
    # next real turn misses; on Anthropic it writes a fresh cache entry, billed at a
    # premium, for a prompt that will never be read again.
    isolate_cache: bool = False


@dataclass(slots=True)
class StreamDelta:
    """One incremental piece of an assistant turn.

    Yielded by `stream_turn` for display. Tool-call arguments are accumulated rather than
    surfaced, because a partial argument object is not something a caller can act on.
    """

    kind: Literal["text", "thinking"] = "text"
    text: str = ""


@dataclass(slots=True)
class ModelChatResult:
    message: ChatMessage
    stop_reason: StopReason = "end_turn"
    usage: TokenUsage | None = None


@runtime_checkable
class ModelProvider(Protocol):
    """Protocol for provider-backed model clients."""

    model_id: str
    route: ModelRoute

    async def chat(
        self,
        messages: list[ChatMessage],
        tools: Sequence[ToolSchema],
        opts: ModelChatOptions | None = None,
    ) -> ModelChatResult: ...

    def stream(
        self,
        messages: list[ChatMessage],
        tools: Sequence[ToolSchema],
        opts: ModelChatOptions | None = None,
    ) -> AsyncIterator[str]: ...


@runtime_checkable
class StreamingModelProvider(ModelProvider, Protocol):
    """A provider that can stream a turn *and* report what it cost.

    The difference matters: `stream()` yields text and nothing else, so a provider that
    implements only it cannot report tool calls *or* usage from a streamed request.
    `record_usage` is the sole feed for `limits.max_input_tokens`, `max_output_tokens` and
    `max_cost_usd`, so a caller must either use `stream_turn` or pay for a second `chat()`
    to meter the turn — see `_stream_one_turn` in `patterns/react.py` and
    `_yield_model_stream` in `patterns/delegating.py`.

    `stream_turn` was once absent from the published contract entirely, so a third-party
    provider could implement everything documented and still land in the unmetered path
    with nothing to say why. It was then added to `ModelProvider` itself, which made the
    opposite claim — that every provider must stream — while every caller went on probing
    for it and the scripted provider, the wire clients and the traced wrapper all treated
    it as optional. A type checker reading that saw a wrapper hiding a mandatory member.

    A separate Protocol says the true thing: streaming is a capability some providers have.
    Probe for it with `supports_stream_turn`, never with `getattr`, so the narrowing is
    visible to the type checker as well as to the reader.
    """

    def stream_turn(
        self,
        messages: list[ChatMessage],
        tools: Sequence[ToolSchema],
        opts: ModelChatOptions | None = None,
    ) -> AsyncIterator[StreamDelta | ModelChatResult]: ...


def supports_stream_turn(client: object) -> TypeIs[StreamingModelProvider]:
    """True when `client` can stream a metered turn.

    One predicate rather than four hand-rolled `getattr(model, "stream_turn", None)`
    checks, so "does this provider stream" has a single answer and the narrowing is a type
    the checker understands. `callable` rather than a plain attribute test: a wrapper that
    forwards attributes can answer to the name without implementing it.

    `TypeIs` rather than `TypeGuard` because callers narrow in both directions —
    `if not supports_stream_turn(client): continue` and then use `client.stream_turn`.
    PEP 647's `TypeGuard` narrows only the positive branch, so that reads as unnarrowed to
    a checker following the spec; `ty` is lenient about it, pyright and mypy are not, and a
    third-party provider author is more likely to run those against `felix_ai`.
    """
    return callable(getattr(client, "stream_turn", None))


# Alias used throughout patterns
ModelClient = ModelProvider


__all__ = [
    "ChatMessage",
    "ContentBlock",
    "ImageAttachment",
    "ModelChatOptions",
    "ModelChatResult",
    "ModelClient",
    "ModelConfig",
    "ModelProvider",
    "ModelRoute",
    "Role",
    "StopReason",
    "StreamDelta",
    "StreamingModelProvider",
    "TokenUsage",
    "ToolCall",
    "ToolSchema",
    "supports_stream_turn",
]
