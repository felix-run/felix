"""Delete whole threads, and find a tenant's threads by id prefix.

`delete_threads` is the one definition of what deleting a thread means -- what a thread owns: its
events, its `thread_state` metadata row, and this process's leaf pointer and head for it, plus the
twin's search index (on Postgres the index is a generated column of the event rows). Whole threads
only: `seq` is dense and `head()` assumes nothing deletes part of a log. The retention sweep's
memory arm deletes through `drop_memory_threads` below; its Postgres arm deletes set-wise for
scale (`jobs/retention.py:_delete_idle_threads`) and owes this list the same rows.

`threads_under` lists the threads whose id starts with a prefix, with when each was last touched:
what a heartbeat's firing-thread retention reads (`jobs/heartbeat.py:prune_firing_threads`). A
prefix is matched as a literal on both arms (`startswith(autoescape=True)` on Postgres), so a `_`
or `%` in it widens nothing.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from felix.config import Settings


async def threads_under(settings: Settings, *, tenant_id: str, prefix: str) -> dict[str, float]:
    """Every thread of `tenant_id` whose id starts with `prefix`, with its last activity in epoch
    seconds: the later of its newest event and its metadata row's last write."""
    from felix.session.store import _memory_session_stores
    from felix.session.thread_state import _use_memory

    if not prefix:
        return {}
    found: dict[str, float] = {}
    if _use_memory(settings):
        from felix.session import thread_state

        store = _memory_session_stores.get(tenant_id)
        for tid, session in (store._sessions if store is not None else {}).items():
            if tid.startswith(prefix) and session._events:
                found[tid] = max(float(ev.ts) for ev in session._events)
        for tid, meta in thread_state._meta_by_thread.items():
            if tid.startswith(prefix):
                # The twin stamps metadata in milliseconds; the Postgres column is seconds.
                found[tid] = max(found.get(tid, 0.0), int(meta.get("updated_at") or 0) / 1000)
        return found

    from sqlalchemy import func, select

    from felix.db.models import SessionEventRow, ThreadState
    from felix.db.session import tenant_session

    async with tenant_session(settings, tenant_id) as db:
        events = await db.execute(
            select(SessionEventRow.thread_id, func.max(SessionEventRow.ts))
            .where(
                SessionEventRow.tenant_id == tenant_id,
                SessionEventRow.thread_id.startswith(prefix, autoescape=True),
            )
            .group_by(SessionEventRow.thread_id)
        )
        for tid, ts in events.all():
            found[str(tid)] = float(ts or 0.0)
        rows = await db.execute(
            select(ThreadState.thread_id, ThreadState.updated_at).where(
                ThreadState.tenant_id == tenant_id,
                ThreadState.thread_id.startswith(prefix, autoescape=True),
            )
        )
        for tid, updated in rows.all():
            found[str(tid)] = max(found.get(str(tid), 0.0), float(updated or 0))
    return found


def drop_memory_threads(tenant_id: str, thread_ids: Iterable[str]) -> tuple[int, int]:
    """The twin's arm of `delete_threads`: every thread named, whole. `(threads, events)` dropped.
    Synchronous, for the retention sweep's memory arm, which is."""
    from felix.session import heads, thread_state, tree
    from felix.session.search import drop_thread_index
    from felix.session.store import _memory_session_stores

    store = _memory_session_stores.get(tenant_id)
    threads = events = 0
    for tid in thread_ids:
        session = store._sessions.pop(tid, None) if store is not None else None
        had_meta = thread_state._meta_by_thread.pop(tid, None) is not None
        if session is not None:
            events += len(session._events)
            session._events.clear()
        heads.note_reset(tenant_id, tid)
        tree.set_leaf(tid, None)
        drop_thread_index(tenant_id=tenant_id, thread_id=tid)
        threads += int(session is not None or had_meta)
    return threads, events


async def delete_threads(settings: Settings, *, tenant_id: str, thread_ids: list[str]) -> int:
    """Delete each thread whole -- its events and its metadata row -- and forget what this process
    holds of it. How many were deleted."""
    from felix.session import heads, tree
    from felix.session.thread_state import _use_memory

    if not thread_ids:
        return 0
    if _use_memory(settings):
        return drop_memory_threads(tenant_id, thread_ids)[0]

    from sqlalchemy import delete

    from felix.db.models import SessionEventRow, ThreadState
    from felix.db.session import tenant_session

    async with tenant_session(settings, tenant_id) as db:
        meta = await db.execute(
            delete(ThreadState)
            .where(ThreadState.tenant_id == tenant_id, ThreadState.thread_id.in_(thread_ids))
            .returning(ThreadState.thread_id)
        )
        events = await db.execute(
            delete(SessionEventRow)
            .where(SessionEventRow.tenant_id == tenant_id, SessionEventRow.thread_id.in_(thread_ids))
            .returning(SessionEventRow.thread_id)
        )
        gone = {str(t) for t in meta.scalars().all()} | {str(t) for t in events.scalars().all()}
        await db.commit()
    for tid in thread_ids:
        # Per process: another replica's cached head for the thread goes stale, as it does after
        # a reset there (`heads.known_head`).
        heads.note_reset(tenant_id, tid)
        tree.set_leaf(tid, None)
    return len(gone)


__all__ = ["delete_threads", "drop_memory_threads", "threads_under"]
