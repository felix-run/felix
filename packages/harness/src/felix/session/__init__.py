"""Felix session seam — event log + strategies + stores."""

from __future__ import annotations

from felix.session.compaction import CompactingSessionStrategy, serialize_conversation
from felix.session.export import events_to_jsonl
from felix.session.snapshot import build_session_metadata, build_snapshot
from felix.session.store import InMemorySessionStore, PostgresSessionStore
from felix.session.strategies import (
    FullReplaySessionStrategy,
    get_session_strategy,
)
from felix.session.thinking import THINKING_LEVELS, budget_for_level, parse_thinking_level
from felix.session.tree import fork_thread, rewind_to
from felix.session.types import (
    AppendableEvent,
    Session,
    SessionEvent,
    SessionStore,
    SessionStrategy,
    WakeState,
    analyze_wake,
    chat_message_to_event,
    event_to_chat_message,
    include_in_llm_context,
)

__all__ = [
    "THINKING_LEVELS",
    "AppendableEvent",
    "CompactingSessionStrategy",
    "FullReplaySessionStrategy",
    "InMemorySessionStore",
    "PostgresSessionStore",
    "Session",
    "SessionEvent",
    "SessionStore",
    "SessionStrategy",
    "WakeState",
    "analyze_wake",
    "budget_for_level",
    "build_session_metadata",
    "build_snapshot",
    "chat_message_to_event",
    "event_to_chat_message",
    "events_to_jsonl",
    "fork_thread",
    "get_session_strategy",
    "include_in_llm_context",
    "parse_thinking_level",
    "rewind_to",
    "serialize_conversation",
]
