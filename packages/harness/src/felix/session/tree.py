"""Session tree helpers — event_id / parent_id / leaf, fork and rewind."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from weakref import WeakValueDictionary

from felix_ai.types import ImageAttachment

from felix.session.types import AppendableEvent, Session, SessionEvent

# In-process leaf pointers: the store itself for memory sessions; on Postgres this process's
# working pointer, set from the row by `sync_leaf` at the start of each turn.
_leaf_by_thread: dict[str, str] = {}
_label_by_event: dict[str, str] = {}

# The stored `leaf_epoch` (`thread_state.LEAF_EPOCH_KEY`) this process's leaf for a thread was
# taken at: set beside the leaf by `sync_leaf` and by a sync'd append from what the store
# resolved, and by `thread_state.persist_leaf` from the epoch it wrote. `store_leaf` writes the
# row only while it still holds this epoch, so an append extending a leaf another replica's
# rewind or fork has since replaced leaves the row alone. Absent for a thread this process never
# resolved, and for stores that keep no epoch (`memory://`, where the lock alone serialises).
_epoch_by_thread: dict[str, int] = {}

# One lock per thread, held while this process reads the stored leaf into the index
# (`sync_leaf`), while it appends and moves the leaf (`annotate_and_append`), and across a
# whole rewind or a fork's write into its destination (`leaf_lock`). Without it a sync could
# read the row between an append's `set_leaf` and its `store_leaf`, and set the index back to
# the row's older leaf -- parenting the turn's next event off its own branch -- and a rewind
# landing there would be overwritten in the row by the append's `store_leaf`. Not reentrant:
# an append made while `leaf_lock` is held passes `lock_held=True`.
# Weak values: a lock lives only while some coroutine holds or waits on it.
_thread_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


def _thread_lock(thread_id: str) -> asyncio.Lock:
    lock = _thread_locks.get(thread_id)
    if lock is None:
        lock = _thread_locks[thread_id] = asyncio.Lock()
    return lock


@asynccontextmanager
async def leaf_lock(session: Session) -> AsyncIterator[None]:
    """Hold ``session``'s thread lock across a multi-step leaf write outside a turn.

    A rewind reads the leaf it abandons, moves this process's leaf, may append a branch
    summary, and stores the result; a fork appends into its destination and stores that leaf.
    Each step alone is safe, the sequence is not: a turn's append landing between them parents
    on the pre-rewind leaf, or stores its own event over the rewind in the row. Appends inside
    the hold go through `annotate_and_append(..., lock_held=True)` -- the lock is not
    reentrant, and taking it again would wait on itself.

    Same process only. A turn on another replica is not serialised by this; the row's
    `leaf_epoch` keeps its appends from overwriting the rewind, see `branch.rewind_and_persist`.
    """
    async with _thread_lock(getattr(session, "id", "") or ""):
        yield


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


def get_leaf_epoch(thread_id: str) -> int | None:
    """The stored epoch this process's leaf for ``thread_id`` was taken at, if it knows one."""
    return _epoch_by_thread.get(thread_id)


def set_leaf_epoch(thread_id: str, epoch: int | None) -> None:
    if not thread_id:
        return
    if epoch is None:
        _epoch_by_thread.pop(thread_id, None)
    else:
        _epoch_by_thread[thread_id] = epoch


def _take_resolved(session: Session, thread_id: str, leaf: str | None) -> None:
    """Set this process's leaf, and the epoch the store resolved it at, from one resolve.

    Only the two resolves that set the index (`sync_leaf`, a sync'd append) take the epoch;
    a read-only `stored_leaf` sets neither, so an export mid-turn cannot advance a turn's
    epoch past a rewind it has not followed.
    """
    set_leaf(thread_id, leaf)
    set_leaf_epoch(thread_id, getattr(session, "resolved_epoch", None))


def set_leaf(thread_id: str, event_id: str | None) -> None:
    if not thread_id:
        return
    if event_id is None:
        _leaf_by_thread.pop(thread_id, None)
    else:
        _leaf_by_thread[thread_id] = event_id


async def stored_leaf(session: Session) -> str | None:
    """The thread's leaf as its store holds it, without touching this process's index.

    For callers that only read the branch: an export, a fork's source, the snapshot, the leaf
    a rewind abandons. A store keeping the leaf durably exposes `resolve_leaf`; one that does
    not (`memory://`, where the index is the store, or a plugin checkpointer) answers the index.
    """
    resolve = getattr(session, "resolve_leaf", None)
    if resolve is None:
        return get_leaf(getattr(session, "id", "") or "")
    return await resolve()


async def sync_leaf(session: Session) -> str | None:
    """Set this process's leaf for ``session`` from its store, and return it.

    Once per turn, before the first append or branch read: `annotate_and_append` parents new
    events on the in-process leaf and `active_branch_events` draws the branch from it, and on
    Postgres that index is per process. Under the thread's lock, so it cannot land inside an
    append that has moved the index but not yet the row.
    """
    thread_id = getattr(session, "id", "") or ""
    async with _thread_lock(thread_id):
        leaf = await stored_leaf(session)
        _take_resolved(session, thread_id, leaf)
        return leaf


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
    *,
    sync: bool = False,
    lock_held: bool = False,
) -> list[str]:
    """Append events with tree linkage; returns new event_ids.

    ``sync`` takes the leaf from the store first, for an append made outside a turn (a route
    adding a label, a name, a custom entry) -- inside the same lock hold as the append, so it
    neither parents on a leaf another replica has moved nor lands inside a turn's append.

    ``lock_held`` is for a caller already inside `leaf_lock` for this thread (a rewind's branch
    summary): the append runs in that hold rather than waiting on the lock it holds.
    """
    thread_id = getattr(session, "id", "") or ""
    if lock_held:
        if not _thread_lock(thread_id).locked():
            # A caller claiming a hold it does not have would append unserialised, silently.
            raise RuntimeError(f"annotate_and_append(lock_held=True) outside leaf_lock for {thread_id!r}")
        return await _append_under_lock(session, thread_id, events, sync=sync)
    async with _thread_lock(thread_id):
        return await _append_under_lock(session, thread_id, events, sync=sync)


async def _append_under_lock(
    session: Session, thread_id: str, events: list[AppendableEvent], *, sync: bool
) -> list[str]:
    if sync:
        _take_resolved(session, thread_id, await stored_leaf(session))
    return await _append_linked(session, thread_id, events)


async def _append_linked(session: Session, thread_id: str, events: list[AppendableEvent]) -> list[str]:
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
        # A store that keeps the leaf durably (Postgres) moves it too. Optional, so a
        # plugin checkpointer's `Session` need not grow a method to keep working.
        store_leaf = getattr(session, "store_leaf", None)
        if store_leaf is not None:
            await store_leaf(ids[-1])
    return ids


async def rewind_to(session: Session, target_event_id: str) -> dict[str, Any]:
    """Move this process's leaf pointer to ``target_event_id`` (must exist on the session).

    One step of a rewind, and it stores nothing: `branch.rewind_and_persist` is the whole
    sequence, under the thread's `leaf_lock`, and what a route calls.
    """
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
    source_leaf = from_event_id or await stored_leaf(source)
    events = await source.get_events()
    branch = active_branch_events(events, session_id=source.id, leaf_id=source_leaf)
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


async def branch_images(session: Session) -> list[tuple[ImageAttachment, str]]:
    """Every image the model's view of this thread holds, oldest first, with where it came from.

    The same events a session strategy renders -- the active branch, in-context events only --
    so a rewind or a fork does not leave an abandoned branch's image nameable, and the images
    are read through `event_to_chat_message`, the one reader of how the log stores them.
    """
    from felix.session.types import event_to_chat_message, include_in_llm_context

    events = active_branch_events(await session.get_events(), session_id=getattr(session, "id", ""))
    found: list[tuple[ImageAttachment, str]] = []
    for event in events:
        if not include_in_llm_context(event):
            continue
        message = event_to_chat_message(event)
        origin = "from the user" if message.role == "user" else f"returned by {message.name or 'a tool'}"
        found.extend((image, origin) for image in message.attachments or () if image.url)
    return found


__all__ = [
    "active_branch_events",
    "annotate_and_append",
    "branch_images",
    "ensure_event_metadata",
    "fork_thread",
    "get_event_id",
    "get_label",
    "get_leaf",
    "get_leaf_epoch",
    "get_parent_id",
    "leaf_lock",
    "new_event_id",
    "rewind_to",
    "set_label",
    "set_leaf",
    "set_leaf_epoch",
    "stored_leaf",
    "sync_leaf",
]
