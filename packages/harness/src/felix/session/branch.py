"""Branch summarization when leaving a path via rewind/fork."""

from __future__ import annotations

import logging
from typing import Any

from felix.patterns.types import ChatMessage
from felix.session.compaction import (
    STRUCTURED_SUMMARY_PROMPT,
    extract_file_ops_from_events,
    serialize_conversation,
)
from felix.session.tree import active_branch_events, get_event_id
from felix.session.types import AppendableEvent, Session, SessionEvent

logger = logging.getLogger("felix.session.branch")


def extract_file_ops(events: list[SessionEvent]) -> dict[str, list[str]]:
    return extract_file_ops_from_events(events)


def abandoned_events(
    events: list[SessionEvent],
    *,
    old_leaf_id: str | None,
    new_leaf_id: str,
    session_id: str = "",
) -> list[SessionEvent]:
    """Events on the old branch from common ancestor (exclusive) to old leaf."""
    if not old_leaf_id or old_leaf_id == new_leaf_id:
        return []
    old_path = active_branch_events(events, session_id=session_id, leaf_id=old_leaf_id)
    new_path = active_branch_events(events, session_id=session_id, leaf_id=new_leaf_id)
    old_ids = [get_event_id(e) for e in old_path]
    new_ids = {get_event_id(e) for e in new_path}
    # Deepest shared ancestor
    common: str | None = None
    for eid in reversed(old_ids):
        if eid and eid in new_ids:
            common = eid
            break
    out: list[SessionEvent] = []
    for e in old_path:
        eid = get_event_id(e)
        if common is None:
            out.append(e)
            continue
        if eid == common:
            continue
        # After common: include until end of old path
        # Walk: if we haven't hit common yet, skip; after common, include.
    # Recompute with explicit walk
    past_common = common is None
    out = []
    for e in old_path:
        eid = get_event_id(e)
        if not past_common:
            if eid == common:
                past_common = True
            continue
        out.append(e)
    return out


async def summarize_abandoned_branch(
    session: Session,
    *,
    old_leaf_id: str | None,
    new_leaf_id: str,
    model: Any | None = None,
    instructions: str | None = None,
) -> dict[str, Any] | None:
    """Append a branch_summary event at the new leaf if there is abandoned work."""
    events = await session.get_events()
    abandoned = abandoned_events(
        events,
        old_leaf_id=old_leaf_id,
        new_leaf_id=new_leaf_id,
        session_id=getattr(session, "id", "") or "",
    )
    if not abandoned:
        return None

    file_ops = extract_file_ops(abandoned)
    summary_text: str | None = None
    usage: dict[str, Any] | None = None

    if model is not None:
        try:
            text = serialize_conversation(abandoned)
            prompt = STRUCTURED_SUMMARY_PROMPT
            if instructions:
                prompt = f"{prompt}\n\nFocus: {instructions}"
            result = await model.chat(
                [
                    ChatMessage(role="system", content=prompt),
                    ChatMessage(role="user", content=text[:120_000]),
                ],
                [],
            )
            summary_text = result.message.content
            if getattr(result, "usage", None):
                u = result.usage
                usage = {
                    "input": getattr(u, "input", 0),
                    "output": getattr(u, "output", 0),
                    "cache_read": getattr(u, "cache_read", 0),
                    "cache_creation": getattr(u, "cache_creation", 0),
                }
        except Exception:
            logger.debug("branch summarization LLM failed", exc_info=True)

    if not summary_text:
        # Deterministic fallback
        n = len(abandoned)
        summary_text = (
            f"## Goal\nAbandoned branch ({n} events).\n\n"
            f"## Progress\n### Done\n- Left path from leaf {old_leaf_id}\n"
        )

    from felix.session.tree import annotate_and_append

    md: dict[str, Any] = {
        "type": "branch_summary",
        "fromId": old_leaf_id,
        "details": file_ops,
    }
    if usage:
        md["usage"] = usage
    ids = await annotate_and_append(
        session,
        [
            AppendableEvent(
                kind="branch_summary",  # type: ignore[arg-type]
                role="system",
                content=summary_text,
                metadata=md,
            )
        ],
    )
    return {
        "ok": True,
        "summary": summary_text,
        "event_id": ids[-1] if ids else None,
        "fromId": old_leaf_id,
        "details": file_ops,
    }


__all__ = [
    "abandoned_events",
    "extract_file_ops",
    "summarize_abandoned_branch",
]
