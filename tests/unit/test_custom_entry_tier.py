"""A client-written custom entry never reaches the model in the system tier, on any path."""

from __future__ import annotations

from felix.session.types import (
    CLIENT_ENTRY_LABEL,
    SessionEvent,
    chat_message_from_parts,
    event_to_chat_message,
    include_in_llm_context,
    retained_turn,
)


def _event(kind: str, role: str, content: str = "note", in_context: bool = True) -> SessionEvent:
    return SessionEvent(
        seq=1, ts=1.0, kind=kind, role=role, content=content, metadata={"in_context": in_context}
    )


def test_a_system_role_custom_entry_is_sent_as_a_labelled_user_turn() -> None:
    e = _event("custom", "system", "operator-looking note")
    assert include_in_llm_context(e)

    sent = event_to_chat_message(e)

    assert sent.role == "user"
    assert sent.content == f"{CLIENT_ENTRY_LABEL}\noperator-looking note"


def test_a_compaction_checkpoint_keeps_the_demotion() -> None:
    # A checkpoint replays what it recorded; recording the stored role would restore the tier.
    e = _event("custom", "system", "operator-looking note")

    replayed = chat_message_from_parts(**retained_turn(e))

    assert replayed == event_to_chat_message(e)
    assert replayed.role == "user"


def test_other_roles_and_kinds_are_unchanged() -> None:
    for kind, role in (
        ("custom", "user"),
        ("custom", "assistant"),
        ("message", "user"),
        ("message", "assistant"),
    ):
        e = _event(kind, role, "plain")
        sent = event_to_chat_message(e)
        assert (sent.role, sent.content) == (role, "plain"), (kind, role)
