"""Session tree helpers — event_id / parent_id / leaf, fork and rewind."""

from __future__ import annotations

import time
import uuid
from typing import Any

from felix.session.types import AppendableEvent, Session, SessionEvent

# In-process leaf pointers for memory sessions (and cache for postgres).
_leaf_by_thread: dict[str, str] = {}
_label_by_event: dict[str, str] = {}


def new_event_id() -> str:
    return uuid.uuid4().hex


def get_event_id(event: SessionEvent) -> str | None:
    if event.metadata:
        return event.metadata.get("event_id")
    return None


def get_parent_id(event: SessionEvent) -> str | None:
    if event.metadata:
        return event.metadata.get("parent_id")
    return None


def ensure_event_metadata(
    metadata: dict[str, Any] | None,
    *,
    parent_id: str | None,
) -> dict[str, Any]:
    md = dict(metadata or {})
    if "event_id" not in md:
        md["event_id"] = new_event_id()
    if parent_id is not None and "parent_id" not in md:
        md["parent_id"] = parent_id
    return md


def get_leaf(thread_id: str) -> str | None:
    return _leaf_by_thread.get(thread_id)


def set_leaf(thread_id: str, event_id: str | None) -> None:
    if not thread_id:
        return
    if event_id is None:
        _leaf_by_thread.pop(thread_id, None)
    else:
        _leaf_by_thread[thread_id] = event_id


def set_label(event_id: str, label: str | None) -> None:
    if label is None:
        _label_by_event.pop(event_id, None)
    else:
        _label_by_event[event_id] = label


def get_label(event_id: str) -> str | None:
    return _label_by_event.get(event_id)


def active_branch_events(
    events: list[SessionEvent],
    *,
    session_id: str = "",
    leaf_id: str | None = None,
) -> list[SessionEvent]:
    """Return the path from root to leaf. Falls back to full linear list if no tree metadata."""
    if not events:
        return []
    has_ids = any(get_event_id(e) for e in events)
    if not has_ids:
        return list(events)

    by_id: dict[str, SessionEvent] = {}
    for e in events:
        eid = get_event_id(e)
        if eid:
            by_id[eid] = e

    leaf = leaf_id or (get_leaf(session_id) if session_id else None)
    if leaf is None or leaf not in by_id:
        # Default leaf = last event with an id
        for e in reversed(events):
            eid = get_event_id(e)
            if eid:
                leaf = eid
                break
    if leaf is None or leaf not in by_id:
        return list(events)

    path: list[SessionEvent] = []
    seen: set[str] = set()
    cur: str | None = leaf
    while cur and cur not in seen:
        seen.add(cur)
        ev = by_id.get(cur)
        if ev is None:
            break
        path.append(ev)
        cur = get_parent_id(ev)
    path.reverse()
    return path


async def annotate_and_append(
    session: Session,
    events: list[AppendableEvent],
) -> list[str]:
    """Append events with tree linkage; returns new event_ids."""
    thread_id = getattr(session, "id", "") or ""
    parent = get_leaf(thread_id)
    ids: list[str] = []
    annotated: list[AppendableEvent] = []
    for ev in events:
        md = ensure_event_metadata(ev.metadata, parent_id=parent)
        eid = str(md["event_id"])
        ids.append(eid)
        annotated.append(
            AppendableEvent(
                kind=ev.kind,
                role=ev.role,
                content=ev.content,
                tool_call_id=ev.tool_call_id,
                name=ev.name,
                tool_calls=ev.tool_calls,
                metadata=md,
                ts=ev.ts,
            )
        )
        parent = eid
    await session.append_batch(annotated)
    if ids and thread_id:
        set_leaf(thread_id, ids[-1])
    return ids


async def rewind_to(session: Session, target_event_id: str) -> dict[str, Any]:
    """Move the leaf pointer to ``target_event_id`` (must exist on the session)."""
    events = await session.get_events()
    ids = {get_event_id(e) for e in events}
    if target_event_id not in ids:
        return {"ok": False, "error": "unknown_event_id"}
    set_leaf(session.id, target_event_id)
    return {"ok": True, "leaf_id": target_event_id, "thread_id": session.id}


async def fork_thread(
    source: Session,
    dest: Session,
    *,
    from_event_id: str | None = None,
) -> dict[str, Any]:
    """Copy the active branch (or path to ``from_event_id``) into ``dest`` as a new linear tree."""
    events = await source.get_events()
    branch = active_branch_events(events, session_id=source.id, leaf_id=from_event_id or get_leaf(source.id))
    if from_event_id:
        # Truncate branch at from_event_id
        trimmed: list[SessionEvent] = []
        for e in branch:
            trimmed.append(e)
            if get_event_id(e) == from_event_id:
                break
        branch = trimmed

    id_map: dict[str, str] = {}
    parent_new: str | None = None
    batch: list[AppendableEvent] = []
    for e in branch:
        old_id = get_event_id(e) or new_event_id()
        new_id = new_event_id()
        id_map[old_id] = new_id
        old_parent = get_parent_id(e)
        mapped_parent = id_map.get(old_parent) if old_parent else parent_new
        md = dict(e.metadata or {})
        md["event_id"] = new_id
        if mapped_parent:
            md["parent_id"] = mapped_parent
        else:
            md.pop("parent_id", None)
        md["forked_from"] = old_id
        batch.append(
            AppendableEvent(
                kind=e.kind,
                role=e.role,
                content=e.content,
                tool_call_id=e.tool_call_id,
                name=e.name,
                tool_calls=e.tool_calls,
                metadata=md,
                ts=e.ts or time.time(),
            )
        )
        parent_new = new_id

    if batch:
        await dest.append_batch(batch)
        set_leaf(dest.id, parent_new)
    return {
        "ok": True,
        "source_thread_id": source.id,
        "thread_id": dest.id,
        "copied": len(batch),
        "leaf_id": parent_new,
    }


__all__ = [
    "active_branch_events",
    "annotate_and_append",
    "ensure_event_metadata",
    "fork_thread",
    "get_event_id",
    "get_label",
    "get_leaf",
    "get_parent_id",
    "new_event_id",
    "rewind_to",
    "set_label",
    "set_leaf",
]
