"""Session — append-only event log outside the model context window."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from felix.patterns.types import ChatMessage, ToolCall

SessionEventKind = Literal[
    "message",
    "tool_call",
    "tool_result",
    "thinking",
    "audit",
    "compaction",
    "model_change",
    "thinking_level_change",
    "branch_summary",
    "custom",
    "label",
    "session_info",
]
# Legacy alias used by a thinner parallel draft.
EventKind = Literal[
    "user",
    "assistant",
    "tool",
    "system",
    "message",
    "tool_result",
    "audit",
    "compaction",
    "model_change",
    "thinking_level_change",
    "branch_summary",
    "custom",
    "label",
    "session_info",
]


@dataclass(slots=True)
class SessionEvent:
    seq: int
    ts: float
    kind: SessionEventKind
    role: Literal["user", "assistant", "system", "tool"] | None = None
    content: str | None = None
    tool_call_id: str | None = None
    name: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    metadata: dict[str, Any] | None = None


# The metadata an event's *shape* needs -- where it sits in the thread tree and, for a summary,
# what it covers -- without its content. `compaction._load_branch` walks the whole log to find
# the active branch and its newest summary, and it read every row's content, tool calls and
# metadata to do it, including each old checkpoint's `retainedTail` copy of the turns it kept.
# Every store that offers `get_event_skeletons` projects through this one list, the in-memory
# twin included, so a key the walk needs and this list lacks fails the suite, not a deployment.
SKELETON_METADATA_KEYS: tuple[str, ...] = (
    "event_id",
    "parent_id",
    "type",
    "covers_to_seq",
    "first_kept_seq",
    "first_kept_entry_id",
    "last_kept_entry_id",
)


def event_skeleton(event: SessionEvent) -> SessionEvent:
    """`event` with no content, tool calls or name, and only `SKELETON_METADATA_KEYS` kept."""
    md = event.metadata or {}
    kept = {k: md[k] for k in SKELETON_METADATA_KEYS if md.get(k) is not None}
    return SessionEvent(seq=event.seq, ts=event.ts, kind=event.kind, role=event.role, metadata=kept or None)


@dataclass(slots=True)
class AppendableEvent:
    kind: SessionEventKind | EventKind
    role: Literal["user", "assistant", "system", "tool"] | None = None
    content: str | None = None
    tool_call_id: str | None = None
    name: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    metadata: dict[str, Any] | None = None
    meta: dict[str, Any] | None = None  # legacy alias
    ts: float | None = None

    def __post_init__(self) -> None:
        if self.meta and not self.metadata:
            self.metadata = self.meta
        # Normalize role-as-kind drafts into message/tool_result.
        if self.kind in {"user", "assistant", "system"} and self.role is None:
            self.role = self.kind  # type: ignore[assignment]
            self.kind = "message"
        elif self.kind == "tool" and self.role is None:
            self.role = "tool"
            self.kind = "tool_result"


@dataclass(slots=True)
class GetEventsOpts:
    from_seq: int | None = None
    to_seq: int | None = None
    limit: int | None = None
    kinds: list[SessionEventKind] | None = None


@dataclass(slots=True)
class WakeState:
    fresh: bool
    head_seq: int
    pending_tool_calls: list[ToolCall] = field(default_factory=list)
    ended_on_assistant: bool = False


@runtime_checkable
class Session(Protocol):
    id: str

    # Both return the sequence numbers they allocated, in order. The writer already
    # computes them under the lock, so handing them back costs nothing and saves the
    # caller re-reading `max(seq)` to learn a number that was just decided.
    async def append(self, event: AppendableEvent) -> int | None: ...
    async def append_batch(self, events: list[AppendableEvent]) -> list[int]: ...
    async def get_events(self, opts: GetEventsOpts | None = None) -> list[SessionEvent]: ...
    async def head(self) -> dict[str, int]: ...
    async def reset(self) -> None: ...
    async def wake(self) -> WakeState: ...


@runtime_checkable
class SessionStore(Protocol):
    def open(self, thread_id: str) -> Session: ...


@dataclass(slots=True)
class SessionRenderOpts:
    system_prompt: str
    model: Any | None = None


@runtime_checkable
class SessionStrategy(Protocol):
    async def render(
        self,
        session: Session,
        incoming: list[ChatMessage],
        opts: SessionRenderOpts | dict[str, Any],
    ) -> list[ChatMessage]: ...


def chat_message_to_event(m: ChatMessage) -> AppendableEvent:
    kind: SessionEventKind = "tool_result" if m.role == "tool" else "message"
    md: dict[str, Any] = {}
    if m.attachments:
        md["attachments"] = [
            {
                "url": a.url,
                "media_type": a.media_type,
                "filename": a.filename,
                "detail": a.detail,
            }
            for a in m.attachments
        ]
    if m.thinking:
        # Signed reasoning has to survive the session round-trip or replay breaks as soon
        # as history is rebuilt from events rather than held in memory for one request.
        md["thinking"] = [dict(b) for b in m.thinking if isinstance(b, dict)]
    return AppendableEvent(
        kind=kind,
        role=m.role,
        content=m.content,
        tool_call_id=m.tool_call_id,
        name=m.name,
        tool_calls=(
            [{"id": tc.id, "name": tc.name, "args": tc.args} for tc in m.tool_calls] if m.tool_calls else None
        ),
        metadata=md or None,
    )


_LLM_SKIP_KINDS = frozenset(
    {
        "audit",
        "compaction",
        "model_change",
        "thinking_level_change",
        "branch_summary",
        "label",
        "session_info",
    }
)


def include_in_llm_context(e: SessionEvent) -> bool:
    """Whether an event should be converted into model context.

    ``custom`` entries are excluded unless ``metadata.in_context`` is true.
    """
    if e.kind == "custom":
        return bool((e.metadata or {}).get("in_context"))
    if e.kind in _LLM_SKIP_KINDS:
        return False
    if e.role == "system":
        return False
    return e.kind in {"message", "tool_result"} or e.role in {
        "user",
        "assistant",
        "tool",
    }


CLIENT_ENTRY_LABEL = "[entry added by the client, not by the operator]"


def _model_role_and_content(e: SessionEvent) -> tuple[str | None, str | None]:
    """The role and text an event reaches the model with.

    One rule, in the one place both live history and a compaction checkpoint read: a
    `custom` entry never enters the system tier. `/chat/sessions/custom` takes its role from
    the caller -- the agent's end user, anonymous on some manifests -- and `in_context` put
    that text beside the operator's own prompt, outranking the caller's own turns. Stored as
    written (a UI may show an operator-styled note); sent to the model as a labelled user
    turn. `assistant` is left alone: a caller can already supply assistant history on `/v1`.
    """
    if e.kind == "custom" and e.role == "system":
        return "user", f"{CLIENT_ENTRY_LABEL}\n{e.content or ''}"
    return e.role, e.content


def event_to_chat_message(e: SessionEvent) -> ChatMessage:
    role, content = _model_role_and_content(e)
    return chat_message_from_parts(
        role=role,
        content=content,
        tool_call_id=e.tool_call_id,
        name=e.name,
        tool_calls=e.tool_calls,
        metadata=e.metadata,
    )


# The metadata `chat_message_from_parts` reads. `retained_turn` copies exactly these into a
# compaction checkpoint, and the round-trip test holds the two together: a key the converter
# learns and this tuple does not is dropped from every replayed turn, the bug that tuple fixed.
_REPLAYED_METADATA_KEYS = ("thinking", "attachments")


def retained_turn(e: SessionEvent) -> dict[str, Any]:
    """A kept turn as a compaction checkpoint stores it: exactly what the converter reads.

    Beside the converter on purpose, so save and load are one module's contract. Attachments
    are copied as stored, so a `data:` image is held twice -- in the log and in the
    checkpoint -- for as long as both live; that is the price of a replay that does not
    depend on the log still holding the turn.
    """
    metadata = e.metadata or {}
    # Stored as the model sees it, so a replayed checkpoint cannot restore a tier the live
    # history would not grant.
    role, content = _model_role_and_content(e)
    return {
        "role": role,
        "content": content,
        "tool_call_id": e.tool_call_id,
        "name": e.name,
        "tool_calls": e.tool_calls,
        "metadata": {k: metadata[k] for k in _REPLAYED_METADATA_KEYS if metadata.get(k)},
    }


def chat_message_from_parts(
    *,
    role: str | None,
    content: str | None,
    tool_call_id: str | None = None,
    name: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    metadata: dict[str, Any] | None = None,
) -> ChatMessage:
    """One stored turn as a model message. The one conversion, for events and checkpoints alike.

    A compaction checkpoint used to rebuild its kept turns by hand and dropped `tool_calls` --
    so every replayed tool result arrived with no call to answer, which Anthropic rejects. Both
    now come through here, so a field added to one is added to both.
    """
    calls = None
    if tool_calls:
        calls = [
            ToolCall(id=str(tc["id"]), name=str(tc["name"]), args=dict(tc.get("args") or {}))
            for tc in tool_calls
        ]
    attachments = None
    raw_atts = (metadata or {}).get("attachments")
    if isinstance(raw_atts, list) and raw_atts:
        from felix.patterns.types import ImageAttachment

        attachments = [
            ImageAttachment(
                url=str(a.get("url") or ""),
                media_type=str(a.get("media_type") or "image/png"),
                filename=a.get("filename"),
                detail=a.get("detail"),
            )
            for a in raw_atts
            if isinstance(a, dict)
        ]
    raw_thinking = (metadata or {}).get("thinking")
    thinking = (
        [b for b in raw_thinking if isinstance(b, dict)]
        if isinstance(raw_thinking, list) and raw_thinking
        else None
    )
    return ChatMessage(
        role=role or "assistant",  # type: ignore[arg-type]
        content=content or "",
        tool_call_id=tool_call_id,
        name=name,
        tool_calls=calls,
        attachments=attachments,
        thinking=thinking,
    )


def analyze_wake(events: list[SessionEvent]) -> WakeState:
    head_seq = len(events)
    turns = [e for e in events if include_in_llm_context(e)]
    if not turns:
        return WakeState(fresh=True, head_seq=head_seq)

    last_assistant_idx = -1
    for i in range(len(turns) - 1, -1, -1):
        e = turns[i]
        if e.role == "assistant" and e.tool_calls:
            last_assistant_idx = i
            break

    pending: list[ToolCall] = []
    if last_assistant_idx >= 0:
        assistant = turns[last_assistant_idx]
        after = turns[last_assistant_idx + 1 :]
        resolved = {e.tool_call_id or "" for e in after if e.kind == "tool_result"}
        for tc in assistant.tool_calls or []:
            if str(tc.get("id")) not in resolved:
                pending.append(
                    ToolCall(
                        id=str(tc["id"]),
                        name=str(tc["name"]),
                        args=dict(tc.get("args") or {}),
                    )
                )

    last = turns[-1]
    ended = last.role == "assistant" and not last.tool_calls
    return WakeState(
        fresh=False,
        head_seq=head_seq,
        pending_tool_calls=pending,
        ended_on_assistant=ended,
    )


__all__ = [
    "CLIENT_ENTRY_LABEL",
    "AppendableEvent",
    "EventKind",
    "GetEventsOpts",
    "Session",
    "SessionEvent",
    "SessionEventKind",
    "SessionRenderOpts",
    "SessionStore",
    "SessionStrategy",
    "WakeState",
    "analyze_wake",
    "chat_message_from_parts",
    "chat_message_to_event",
    "event_to_chat_message",
    "include_in_llm_context",
    "retained_turn",
]
