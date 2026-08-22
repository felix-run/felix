"""Felix session seam — event log + strategies + stores."""

from __future__ import annotations

from felix.session.store import InMemorySessionStore, PostgresSessionStore
from felix.session.strategies import (
    FullReplaySessionStrategy,
    get_session_strategy,
)
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
    "get_session_strategy",
]
