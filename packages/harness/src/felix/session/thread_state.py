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
from felix.session.tree import set_leaf_epoch as _set_leaf_epoch

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from felix.db.models import ThreadState
    from felix.session.types import Session

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


# Counts the times a rewind or a fork has moved a row's leaf (`persist_leaf`), beside
# `leaf_v` in `labels_json`. An append's `store_leaf` writes only while the row is still at the
# epoch its turn resolved the leaf at (`tree.sync_leaf`), so a rewind committed by another
# replica mid-turn is not overwritten by the turn's next append. Appends and `adopt_leaf` never
# bump it: two turns racing on two replicas stay last-writer-wins, and a transient failure of
# one `store_leaf` leaves the epoch -- and so every later append's condition -- where it was.
# A row written before the key existed reads as 0, which is also what a fresh row starts at.
LEAF_EPOCH_KEY = "leaf_epoch"


# What `GET /chat/sessions` shows a client to recognise a thread by, beside its name. Both are
# stashed in the metadata at write time so listing a tenant's threads stays one query over
# `thread_state` rather than a read of every thread's log. A thread written before they existed
# lacks both and lists them as null.
#
# `preview` is the thread's first user message, masked and whitespace-collapsed, cut to
# `PREVIEW_CHARS` -- written once (`note_first_message`) and never moved by a later turn.
# `last_manifest` is the manifest the thread's newest turn ran under (`manifests.pin`), where
# `manifest_name` is the pin's own record of the *first*; a row older than this key falls back
# to that.
PREVIEW_KEY = "preview"
LAST_MANIFEST_KEY = "last_manifest"
PREVIEW_CHARS = 120


def thread_preview(text: str | None) -> str | None:
    """``text`` as a one-line preview: whitespace collapsed, cut to `PREVIEW_CHARS`, or None.

    Cut after collapsing, so a pasted block of blank lines does not spend the budget, and the
    cut ends in an ellipsis so a client can tell a short message from a truncated one.
    """
    if not text:
        return None
    flat = " ".join(text.split())
    if not flat:
        return None
    if len(flat) <= PREVIEW_CHARS:
        return flat
    return flat[: PREVIEW_CHARS - 1].rstrip() + "\u2026"


def masked_preview(text: str | None) -> str | None:
    """``text`` as the stored `preview`: masked (`secrets.redact_text`) first, then `thread_preview`.

    The one rule both writers apply -- a turn's `note_first_message` and the backfill's
    `backfill_preview` -- so a thread listed from a backfill reads exactly as it would had
    the turn recorded it. Masked before the cut, so a secret straddling the cut is masked
    rather than half-kept.
    """
    from felix.secrets import redact_text

    return thread_preview(redact_text(text) if text else text)


def leaf_epoch(labels_json: dict[str, Any] | None) -> int:
    """The row's rewind/fork epoch; 0 for a row that never had one."""
    try:
        return int((labels_json or {}).get(LEAF_EPOCH_KEY) or 0)
    except TypeError, ValueError:
        return 0


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
    meta.pop(LEAF_EPOCH_KEY, None)
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
    """Move a thread's leaf, leaving every other metadata key as the store has it.

    A branch move -- a rewind, or a fork writing its new thread -- so on Postgres it bumps the
    row's `leaf_epoch` in the same locked write, and this process's leaf takes that epoch: a
    turn on another replica that resolved the leaf before this commit stops storing its
    appends' leaf over it (`_PostgresSession.store_leaf`), and a turn here, which the thread's
    `leaf_lock` already orders after this, goes on storing on top of it.
    """
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
        epoch = leaf_epoch(stored) + 1
        stored[LEAF_EPOCH_KEY] = epoch
        row.leaf_event_id = leaf_event_id
        row.labels_json = stored
        row.updated_at = int(time.time())
        await db.commit()
    _set_leaf_epoch(thread_id, epoch)


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


async def thread_exists(
    session: Session,
    *,
    settings: Settings | None,
    tenant_id: str,
) -> bool:
    """Whether ``session``'s thread has anything stored: an event, or session metadata.

    Either alone is a thread. Appends write no metadata row, so a thread with a turn in it
    may have no row at all; and a rename, an abort or a thinking change writes a row without
    an event. Reads only, on both arms -- an unknown id gains neither an entry nor a row.
    """
    if (await session.head()).get("seq", 0) > 0:
        return True
    thread_id = session.id
    if _use_memory(settings):
        return thread_id in _meta_by_thread
    assert settings is not None
    return await _read_row(settings, tenant_id, thread_id) is not None


async def claim_thread(
    *,
    settings: Settings | None,
    tenant_id: str,
    thread_id: str,
    **fields: Any,
) -> bool:
    """Create the thread's metadata with ``fields`` only if it has none; whether this call did.

    The claim is the write itself -- `INSERT … ON CONFLICT DO NOTHING` on Postgres -- so of two
    replicas claiming one id at once exactly one is told it won, which no in-process lock can
    say. Says nothing about events: a thread with events and no row is claimable, so a caller
    that means "new thread" asks `thread_exists` first.
    """
    if _use_memory(settings):
        if thread_id in _meta_by_thread:
            return False
        _merge_fields(_mem_meta(thread_id), fields)
        return True
    assert settings is not None
    from sqlalchemy.dialects.postgresql import insert

    from felix.db.models import ThreadState
    from felix.db.session import tenant_session

    meta = _default_meta()
    _merge_fields(meta, fields)
    async with tenant_session(settings, tenant_id) as db:
        inserted = await db.scalar(
            insert(ThreadState)
            .values(
                tenant_id=tenant_id,
                thread_id=thread_id,
                leaf_event_id=None,
                labels_json=meta,
                updated_at=int(time.time()),
            )
            .on_conflict_do_nothing(index_elements=["tenant_id", "thread_id"])
            .returning(ThreadState.thread_id)
        )
        await db.commit()
    return inserted is not None


def _str_or_none(value: Any) -> str | None:
    return str(value) if value else None


def _session_index_dict(
    thread_id: str, meta: dict[str, Any], fallback_ms: int | None = None
) -> dict[str, Any]:
    """One row of `GET /chat/sessions`, from a thread's stored metadata -- both arms.

    ``fallback_ms`` stands in for a row's missing timestamps (the Postgres arm passes the row's
    own `updated_at`). Every key is spelled out: this literal is the response's contract.
    """
    return {
        "id": thread_id,
        "createdAt": meta.get("created_at") or fallback_ms,
        "updatedAt": meta.get("updated_at") or fallback_ms,
        "parentSessionId": meta.get("parent_session_id"),
        "sessionName": meta.get("session_name"),
        "preview": _str_or_none(meta.get(PREVIEW_KEY)),
        "manifest": _str_or_none(meta.get(LAST_MANIFEST_KEY) or meta.get("manifest_name")),
    }


async def list_thread_metadata(
    *,
    settings: Settings | None,
    tenant_id: str,
) -> list[dict[str, Any]]:
    """List durable session metadata for a tenant -- `GET /chat/sessions`, one query."""
    items: list[dict[str, Any]] = []
    if _use_memory(settings):
        for tid, meta in _meta_by_thread.items():
            if tid.startswith(f"{tenant_id}:") or tenant_id == "default":
                items.append(_session_index_dict(tid, meta))
        return items

    from sqlalchemy import select

    from felix.db.models import ThreadState
    from felix.db.session import get_session_factory

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        rows = (await db.scalars(select(ThreadState).where(ThreadState.tenant_id == tenant_id))).all()
        for row in rows:
            items.append(_session_index_dict(row.thread_id, row.labels_json or {}, row.updated_at * 1000))
    return items


async def note_first_message(
    *,
    settings: Settings | None,
    tenant_id: str,
    thread_id: str,
    text: str | None,
) -> bool:
    """Record ``text`` as the thread's `preview` unless it already has one; whether this did.

    Called with every turn's first user message, so the common case -- a thread that already
    has its preview -- must cost as little as possible: a primary-key read on Postgres and
    nothing written. Only a thread without one takes the row lock, and it checks again under
    it, so two first turns racing on two replicas keep whichever committed first.

    The text is masked with the same rule the session log applies on the way in
    (`secrets.redact_text`), before it is cut, so the metadata never holds what the log would
    not -- and a secret straddling the cut is masked rather than half-kept (`masked_preview`).
    """
    preview = masked_preview(text)
    if preview is None:
        return False
    if _use_memory(settings):
        current = _meta_by_thread.get(thread_id)
        if current is not None and current.get(PREVIEW_KEY):
            return False
        meta = _mem_meta(thread_id)
        meta[PREVIEW_KEY] = preview
        _bump(meta)
        return True

    assert settings is not None
    row = await _read_row(settings, tenant_id, thread_id)
    if row is not None and (row.labels_json or {}).get(PREVIEW_KEY):
        return False
    from felix.db.session import tenant_session

    async with tenant_session(settings, tenant_id) as db:
        # The same new-row leaf as `update_thread_meta`, for the same reason.
        row = await _locked_row(
            db, tenant_id=tenant_id, thread_id=thread_id, leaf_event_id=_mem_get_leaf(thread_id)
        )
        stored = dict(row.labels_json or {})
        if stored.get(PREVIEW_KEY):
            await db.rollback()
            return False
        stored[PREVIEW_KEY] = preview
        _bump(stored)
        row.labels_json = stored
        row.updated_at = int(time.time())
        await db.commit()
    return True


# --- the preview backfill's store half (`session/preview_backfill.py`) ------------------------
#
# A thread written before `preview` existed lists as null until something records one. These
# are the reads and the one write the backfill needs, on both arms. The write is not
# `note_first_message`: that is a turn, so it moves `updated_at` -- which reorders a client's
# thread list and, on Postgres, is the column retention reads to spare a thread from the idle
# sweep (`jobs/retention.py:_delete_idle_threads`). Backfilling every old thread through it
# would make all of them look touched today and keep them a full retention window longer. And
# it inserts a missing row, where the backfill must never re-create a row retention just
# deleted.


async def list_thread_tenants(*, settings: Settings | None) -> list[str]:
    """Every tenant holding session metadata, sorted. Cross-tenant: Postgres reads under `rls_bypass`."""
    if _use_memory(settings):
        return sorted({tid.split(":", 1)[0] for tid in _meta_by_thread if ":" in tid})
    assert settings is not None
    from sqlalchemy import select

    from felix.db.models import ThreadState
    from felix.db.session import get_session_factory, rls_bypass

    with rls_bypass():
        async with get_session_factory(settings=settings)() as db:
            rows = await db.scalars(select(ThreadState.tenant_id).distinct().order_by(ThreadState.tenant_id))
            return list(rows.all())


async def threads_missing_preview(
    *,
    settings: Settings | None,
    tenant_id: str,
    after: str | None = None,
    limit: int = 200,
) -> list[str]:
    """One page of ``tenant_id``'s thread ids with no `preview`, in id order, after ``after``.

    Keyset rather than offset, so a page costs the same however far in it is, and a thread
    filled between two pages cannot shift the next one. A null, absent or empty preview all
    count as missing, which is what `_session_index_dict` lists as null.
    """
    if _use_memory(settings):
        prefix = f"{tenant_id}:"
        ids = sorted(
            tid
            for tid, meta in _meta_by_thread.items()
            if tid.startswith(prefix) and not meta.get(PREVIEW_KEY) and (after is None or tid > after)
        )
        return ids[:limit]
    assert settings is not None
    from sqlalchemy import func, select

    from felix.db.models import ThreadState
    from felix.db.session import tenant_session

    stmt = select(ThreadState.thread_id).where(
        ThreadState.tenant_id == tenant_id,
        func.coalesce(ThreadState.labels_json[PREVIEW_KEY].astext, "") == "",
    )
    if after is not None:
        stmt = stmt.where(ThreadState.thread_id > after)
    async with tenant_session(settings, tenant_id) as db:
        rows = await db.scalars(stmt.order_by(ThreadState.thread_id).limit(limit))
        return list(rows.all())


async def backfill_preview(
    *,
    settings: Settings | None,
    tenant_id: str,
    thread_id: str,
    text: str | None,
) -> bool:
    """Record ``text`` as an existing thread's `preview` if it still has none; whether this did.

    Through `masked_preview`, as a turn would have recorded it. Leaves `updated_at` (both the
    metadata's and the Postgres column) where it was, and never creates a thread: an id with
    no metadata, or one deleted since it was listed, is left alone. On Postgres it takes the
    row's lock for one short transaction and checks again under it, so a turn that recorded a
    preview in the meantime keeps its own. `revision` still counts the write, so a client that
    watches it sees the metadata changed.
    """
    preview = masked_preview(text)
    if preview is None:
        return False
    if _use_memory(settings):
        meta = _meta_by_thread.get(thread_id)
        if meta is None or meta.get(PREVIEW_KEY):
            return False
        meta[PREVIEW_KEY] = preview
        meta["revision"] = int(meta.get("revision") or 0) + 1
        return True

    assert settings is not None
    from sqlalchemy import select

    from felix.db.models import ThreadState
    from felix.db.session import tenant_session

    async with tenant_session(settings, tenant_id) as db:
        row = (
            await db.execute(
                select(ThreadState)
                .where(ThreadState.tenant_id == tenant_id, ThreadState.thread_id == thread_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        stored = dict(row.labels_json or {}) if row is not None else {}
        if row is None or stored.get(PREVIEW_KEY):
            await db.rollback()
            return False
        stored[PREVIEW_KEY] = preview
        stored["revision"] = int(stored.get("revision") or 0) + 1
        row.labels_json = stored
        await db.commit()
    return True


def reset_thread_meta_for_tests() -> None:
    _meta_by_thread.clear()


__all__ = [
    "LAST_MANIFEST_KEY",
    "LEAF_EPOCH_KEY",
    "LEAF_TRACKED_KEY",
    "LEAF_TRACKED_VERSION",
    "PREVIEW_CHARS",
    "PREVIEW_KEY",
    "backfill_preview",
    "claim_thread",
    "get_thread_meta",
    "leaf_epoch",
    "leaf_is_tracked",
    "list_thread_metadata",
    "list_thread_tenants",
    "load_leaf",
    "masked_preview",
    "note_first_message",
    "persist_leaf",
    "reset_thread_meta_for_tests",
    "thread_exists",
    "thread_preview",
    "threads_missing_preview",
    "update_thread_meta",
]
