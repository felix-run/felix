"""Session — append-only event log outside the model context window."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from felix.patterns.types import ChatMessage, ToolCall

SessionEventKind = Literal[
    "message", "tool_call", "tool_result", "thinking", "audit", "compaction", "model_change"
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

    async def append(self, event: AppendableEvent) -> None: ...
    async def append_batch(self, events: list[AppendableEvent]) -> None: ...
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
    return AppendableEvent(
        kind=kind,
        role=m.role,
        content=m.content,
        tool_call_id=m.tool_call_id,
        name=m.name,
        tool_calls=(
            [{"id": tc.id, "name": tc.name, "args": tc.args} for tc in m.tool_calls]
            if m.tool_calls
            else None
        ),
    )


def event_to_chat_message(e: SessionEvent) -> ChatMessage:
    tool_calls = None
    if e.tool_calls:
        tool_calls = [
            ToolCall(id=str(tc["id"]), name=str(tc["name"]), args=dict(tc.get("args") or {}))
            for tc in e.tool_calls
        ]
    return ChatMessage(
        role=e.role or "assistant",  # type: ignore[arg-type]
        content=e.content or "",
        tool_call_id=e.tool_call_id,
        name=e.name,
        tool_calls=tool_calls,
    )


def analyze_wake(events: list[SessionEvent]) -> WakeState:
    head_seq = len(events)
    turns = [e for e in events if e.kind not in {"audit", "compaction", "model_change"}]
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
    "chat_message_to_event",
    "event_to_chat_message",
]
