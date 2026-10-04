"""Extend thread_state — leaf, labels, session name, phase, thinking level.

Two arms, and on each exactly one source of truth. On `memory://` that is `_meta_by_thread`
plus the tree module's leaf index. On Postgres it is the `thread_state` row, read on every
read and written under a row lock on every write.

The Postgres arm used to treat `_meta_by_thread` as the truth with the row as a mirror, and
the cache is per process. A replica that had never seen a thread wrote defaults over every
field it was not changing; one that had seen it never saw another replica's writes again;
`revision` counted that process's writes; and two first-inserts of one thread raced into an
IntegrityError. A primary-key read costs less than any of those.

The tree module's in-process leaf is still what new events parent on and what the active
branch is drawn from, so on Postgres it is set from the row at the start of every turn
(`tree.sync_leaf` -> `_PostgresSession.resolve_leaf`) and moved by the turn's own appends.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from felix.config import Settings
from felix.session.tree import get_leaf as _mem_get_leaf
from felix.session.tree import set_label as _mem_set_label
from felix.session.tree import set_leaf as _mem_set_leaf

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from felix.db.models import ThreadState

# The memory arm's store, keyed by the tenant-prefixed thread id (`thread_ids.py` composes
# every one, and a tenant id cannot contain the delimiter, so the prefix is unambiguous).
# The Postgres arm neither reads nor writes it.
_meta_by_thread: dict[str, dict[str, Any]] = {}

# Fields a caller clears by passing `None`; any other `None` is "leave it alone".
_NULLABLE = frozenset({"session_name", "parent_session_id", "model_id"})

# Set in a row's `labels_json` by every writer that keeps `leaf_event_id` current: an append
# (`_PostgresSession.store_leaf`), a fork or a rewind (`persist_leaf`). A row without it was
# written before the stored leaf followed appends, when only fork and rewind wrote it -- so its
# leaf can be a rewind target the conversation has since moved past, and only a marked row's
# leaf is the leaf. A key rather than a comparison with the log because the log cannot tell
# the two apart: after a deliberate rewind the newest event is on the abandoned branch too.
LEAF_TRACKED_KEY = "leaf_v"
LEAF_TRACKED_VERSION = 2


def leaf_is_tracked(labels_json: dict[str, Any] | None) -> bool:
    """Whether a row's `leaf_event_id` was written by a writer that keeps it current."""
    return (labels_json or {}).get(LEAF_TRACKED_KEY) == LEAF_TRACKED_VERSION


def _default_meta() -> dict[str, Any]:
    now_ms = int(time.time() * 1000)
    return {
        "session_name": None,
        "phase": "idle",
        "thinking_level": "off",
        "model_id": None,
        "parent_session_id": None,
        "labels": {},
        "created_at": now_ms,
        "updated_at": now_ms,
        "revision": 0,
    }


def _mem_meta(thread_id: str) -> dict[str, Any]:
    """Get-or-create a thread's entry. Write paths only -- the entry is what lists a session.

    `list_thread_metadata` on `memory://` lists exactly these keys, the way the Postgres arm
    lists `thread_state` rows, so creating one is creating a session. A read that called this
    turned every unknown id it was asked about into an empty, id-titled session; reads use
    `_meta_by_thread.get` instead.
    """
    if thread_id not in _meta_by_thread:
        _meta_by_thread[thread_id] = _default_meta()
    return _meta_by_thread[thread_id]


def _use_memory(settings: Settings | None) -> bool:
    if settings is None:
        return True
    url = settings.database_url
    return ":memory:" in url or "sqlite" in url or url.startswith("memory://")


def _merged_labels(current: dict[str, Any], change: dict[str, Any]) -> dict[str, Any]:
    labels = {**current, **change}
    # None clears a label
    for k, v in list(labels.items()):
        if v is None:
            labels.pop(k, None)
            _mem_set_label(k, None)
        else:
            _mem_set_label(k, str(v))
    return labels


def _merged_feedback(current: dict[str, Any], change: dict[str, Any]) -> dict[str, Any]:
    # Same merge as labels: keyed by event id, `None` clears one.
    feedback = dict(current)
    for k, v in change.items():
        if v is None:
            feedback.pop(k, None)
        else:
            feedback[k] = v
    return feedback


def _merge_fields(meta: dict[str, Any], fields: dict[str, Any]) -> None:
    """Apply a write's fields to ``meta`` in place -- the one merge rule both arms use."""
    for key, value in fields.items():
        if key == "labels" and isinstance(value, dict):
            meta["labels"] = _merged_labels(meta.get("labels") or {}, value)
        elif key == "feedback" and isinstance(value, dict):
            meta["feedback"] = _merged_feedback(meta.get("feedback") or {}, value)
        elif value is not None or key in _NULLABLE:
            meta[key] = value


def _bump(meta: dict[str, Any]) -> None:
    meta["updated_at"] = int(time.time() * 1000)
    meta["revision"] = int(meta.get("revision") or 0) + 1


def _row_meta(stored: dict[str, Any] | None) -> dict[str, Any]:
    """Defaults under a row's own keys, so a key added to `_default_meta` later still answers."""
    meta = _default_meta()
    meta.update(stored or {})
    # Bookkeeping for the leaf column, not session metadata.
    meta.pop(LEAF_TRACKED_KEY, None)
    for key in ("labels", "feedback"):
        if isinstance(meta.get(key), dict):
            meta[key] = dict(meta[key])
    return meta


async def _locked_row(
    db: AsyncSession,
    *,
    tenant_id: str,
    thread_id: str,
    leaf_event_id: str | None,
) -> ThreadState:
    """The thread's row, locked until this transaction ends -- inserted first if missing.

    `INSERT … ON CONFLICT DO NOTHING` and then `SELECT … FOR UPDATE`, so two replicas
    writing a thread neither of them has seen both get the one row, the second waiting on
    the first's lock and then reading what it wrote. ``leaf_event_id`` is only the new
    row's leaf; an existing row keeps its own.
    """
    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert

    from felix.db.models import ThreadState

    await db.execute(
        insert(ThreadState)
        .values(
            tenant_id=tenant_id,
            thread_id=thread_id,
            leaf_event_id=leaf_event_id,
            labels_json=_default_meta(),
            updated_at=int(time.time()),
        )
        .on_conflict_do_nothing(index_elements=["tenant_id", "thread_id"])
    )
    stmt = (
        select(ThreadState)
        .where(ThreadState.tenant_id == tenant_id, ThreadState.thread_id == thread_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return (await db.execute(stmt)).scalar_one()


async def _read_row(settings: Settings, tenant_id: str, thread_id: str) -> ThreadState | None:
    from felix.db.models import ThreadState
    from felix.db.session import tenant_session

    async with tenant_session(settings, tenant_id) as db:
        return await db.get(ThreadState, (tenant_id, thread_id))


async def persist_leaf(
    *,
    settings: Settings | None,
    tenant_id: str,
    thread_id: str,
    leaf_event_id: str | None,
) -> None:
    """Move a thread's leaf, leaving every other metadata key as the store has it."""
    # This process's working pointer, which `tree.annotate_and_append` parents new events
    # on. On Postgres it is written through here and never read back as the stored leaf.
    _mem_set_leaf(thread_id, leaf_event_id)
    if _use_memory(settings):
        _bump(_mem_meta(thread_id))
        return
    assert settings is not None
    from felix.db.session import tenant_session

    async with tenant_session(settings, tenant_id) as db:
        row = await _locked_row(db, tenant_id=tenant_id, thread_id=thread_id, leaf_event_id=leaf_event_id)
        stored = dict(row.labels_json or {})
        _bump(stored)
        stored[LEAF_TRACKED_KEY] = LEAF_TRACKED_VERSION
        row.leaf_event_id = leaf_event_id
        row.labels_json = stored
        row.updated_at = int(time.time())
        await db.commit()


async def load_leaf(
    *,
    settings: Settings | None,
    tenant_id: str,
    thread_id: str,
) -> str | None:
    if _use_memory(settings):
        return _mem_get_leaf(thread_id)
    assert settings is not None
    row = await _read_row(settings, tenant_id, thread_id)
    return row.leaf_event_id if row is not None else None


async def update_thread_meta(
    *,
    settings: Settings | None,
    tenant_id: str,
    thread_id: str,
    **fields: Any,
) -> dict[str, Any]:
    """Merge session metadata (name, phase, thinking_level, model_id, labels, …)."""
    if _use_memory(settings):
        meta = _mem_meta(thread_id)
        _merge_fields(meta, fields)
        _bump(meta)
        return dict(meta)

    assert settings is not None
    from felix.db.session import tenant_session

    async with tenant_session(settings, tenant_id) as db:
        # A new row starts at this process's leaf: no replica has stored one for the thread,
        # so the one this process holds is all there is. It is not marked tracked -- this
        # process may not have served the thread's latest append -- so the next turn's
        # `resolve_leaf` takes the log's newest event over it. An existing row keeps its own.
        row = await _locked_row(
            db, tenant_id=tenant_id, thread_id=thread_id, leaf_event_id=_mem_get_leaf(thread_id)
        )
        stored = dict(row.labels_json or {})
        _merge_fields(stored, fields)
        _bump(stored)
        row.labels_json = stored
        row.updated_at = int(time.time())
        await db.commit()
    return _row_meta(stored)


async def get_thread_meta(
    *,
    settings: Settings | None,
    tenant_id: str,
    thread_id: str,
) -> dict[str, Any]:
    if _use_memory(settings):
        # A read: an unknown thread answers defaults without gaining an entry, as Postgres
        # answers a missing `thread_state` row without inserting one.
        meta = _meta_by_thread.get(thread_id)
        return dict(meta) if meta is not None else _default_meta()
    assert settings is not None
    row = await _read_row(settings, tenant_id, thread_id)
    return _row_meta(row.labels_json if row is not None else None)


async def list_thread_metadata(
    *,
    settings: Settings | None,
    tenant_id: str,
) -> list[dict[str, Any]]:
    """List durable session metadata for a tenant."""
    items: list[dict[str, Any]] = []
    if _use_memory(settings):
        for tid, meta in _meta_by_thread.items():
            if tid.startswith(f"{tenant_id}:") or tenant_id == "default":
                items.append(
                    {
                        "id": tid,
                        "createdAt": meta.get("created_at"),
                        "updatedAt": meta.get("updated_at"),
                        "parentSessionId": meta.get("parent_session_id"),
                        "sessionName": meta.get("session_name"),
                    }
                )
        return items

    from sqlalchemy import select

    from felix.db.models import ThreadState
    from felix.db.session import get_session_factory

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        rows = (await db.scalars(select(ThreadState).where(ThreadState.tenant_id == tenant_id))).all()
        for row in rows:
            lj = row.labels_json or {}
            items.append(
                {
                    "id": row.thread_id,
                    "createdAt": lj.get("created_at") or row.updated_at * 1000,
                    "updatedAt": lj.get("updated_at") or row.updated_at * 1000,
                    "parentSessionId": lj.get("parent_session_id"),
                    "sessionName": lj.get("session_name"),
                }
            )
    return items


def reset_thread_meta_for_tests() -> None:
    _meta_by_thread.clear()


__all__ = [
    "LEAF_TRACKED_KEY",
    "LEAF_TRACKED_VERSION",
    "get_thread_meta",
    "leaf_is_tracked",
    "list_thread_metadata",
    "load_leaf",
    "persist_leaf",
    "reset_thread_meta_for_tests",
    "update_thread_meta",
]
