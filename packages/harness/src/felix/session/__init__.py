"""Felix session seam — event log + strategies + stores."""

from __future__ import annotations

from felix.session.compaction import CompactingSessionStrategy
from felix.session.store import InMemorySessionStore, PostgresSessionStore
from felix.session.strategies import (
    FullReplaySessionStrategy,
    get_session_strategy,
)
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
)

__all__ = [
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
    "chat_message_to_event",
    "event_to_chat_message",
    "fork_thread",
    "get_session_strategy",
    "rewind_to",
]
