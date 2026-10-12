"""The file-checkpoint ledger: the `file_checkpoints` table, its in-memory twin, and the content
the captures name in the object store (`checkpoints/{tenant}/{sha256}`).

`tools/workspace_checkpoints.py` takes the captures and restores from them; this is where they are
kept. Content is shared by digest across a tenant's captures, so it is deleted only when no capture
names it any more -- content first, then rows, so drift is a row naming absent content (a restore
reports it skipped), never content no row names. The retention sweep reads a file's whole history
in a thread at once (`expired_paths`): sweeping some of a file's captures would restore it to an
in-between state and call it restored.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from felix.db.session import _use_memory

if TYPE_CHECKING:
    from felix.config import Settings

# The most captures a restore reads; past it the answer says `truncated`.
MAX_RESTORE_CAPTURES = 2_000


#: `(tenant_id, thread_id, capture_id) -> row`, the in-memory twin of `file_checkpoints`.
_rows: dict[tuple[str, str, str], dict[str, Any]] = {}


def clear_memory_ledger() -> None:
    """Drop the in-memory ledger. Test seam, matching the other `memory://` stores."""
    _rows.clear()


def now_ms() -> int:
    return int(time.time() * 1000)


def checkpoint_key(tenant_id: str, sha256: str) -> str:
    """Where a tenant's kept content with this digest lives. One object however many captures."""
    return f"checkpoints/{tenant_id}/{sha256}"


def _capture_id() -> str:
    """Time-ordered, so a thread's captures sort in the order they were taken."""
    return f"{time.time_ns():020d}{secrets.token_hex(4)}"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- the ledger ------------------------------------------------------------------------------


async def _insert(settings: Settings, row: dict[str, Any]) -> None:
    if _use_memory(settings):
        _rows[(row["tenant_id"], row["thread_id"], row["capture_id"])] = dict(row)
        return
    from felix.db.models import FileCheckpointRow
    from felix.db.session import tenant_session

    async with tenant_session(settings, row["tenant_id"]) as db:
        db.add(FileCheckpointRow(**row))
        await db.commit()


async def _update(settings: Settings, tenant_id: str, thread_id: str, capture_id: str, **values: Any) -> None:
    if _use_memory(settings):
        row = _rows.get((tenant_id, thread_id, capture_id))
        if row is not None:
            row.update(values)
        return
    from sqlalchemy import update

    from felix.db.models import FileCheckpointRow
    from felix.db.session import tenant_session

    async with tenant_session(settings, tenant_id) as db:
        await db.execute(
            update(FileCheckpointRow)
            .where(
                FileCheckpointRow.tenant_id == tenant_id,
                FileCheckpointRow.thread_id == thread_id,
                FileCheckpointRow.capture_id == capture_id,
            )
            .values(**values)
        )
        await db.commit()


async def captures(
    settings: Settings,
    tenant_id: str,
    thread_id: str,
    anchors: Iterable[str],
    limit: int = MAX_RESTORE_CAPTURES,
) -> list[dict[str, Any]]:
    """The thread's captures anchored at any of `anchors`, oldest first, at most `limit`."""
    wanted = set(anchors)
    if not wanted:
        return []
    if _use_memory(settings):
        rows = [
            dict(r)
            for (t, th, _), r in _rows.items()
            if t == tenant_id and th == thread_id and r["anchor_event_id"] in wanted
        ]
        return sorted(rows, key=lambda r: r["capture_id"])[:limit]
    from sqlalchemy import select

    from felix.db.models import FileCheckpointRow
    from felix.db.session import tenant_session

    async with tenant_session(settings, tenant_id) as db:
        result = await db.execute(
            select(FileCheckpointRow)
            .where(
                FileCheckpointRow.tenant_id == tenant_id,
                FileCheckpointRow.thread_id == thread_id,
                FileCheckpointRow.anchor_event_id.in_(sorted(wanted)),
            )
            .order_by(FileCheckpointRow.capture_id)
            .limit(limit)
        )
        return [_row_dict(r) for r in result.scalars().all()]


def _row_dict(row: Any) -> dict[str, Any]:
    return {c: getattr(row, c) for c in _COLUMNS}


_COLUMNS = (
    "tenant_id",
    "thread_id",
    "capture_id",
    "anchor_event_id",
    "scope",
    "path",
    "before_state",
    "before_sha",
    "after_state",
    "after_sha",
    "size_bytes",
    "created_at",
)


async def kept_bytes(settings: Settings, tenant_id: str) -> int:
    """The bytes of content this tenant's captures name in the store, each digest counted once:
    every `stored` capture carries its content's size, so content another capture still names is
    still counted after the capture that first kept it is gone."""
    if _use_memory(settings):
        sizes: dict[str, int] = {}
        for (t, _, _), r in _rows.items():
            if t == tenant_id and r["before_state"] == "stored" and r["before_sha"]:
                sizes[r["before_sha"]] = max(sizes.get(r["before_sha"], 0), int(r["size_bytes"]))
        return sum(sizes.values())
    from sqlalchemy import func, select

    from felix.db.models import FileCheckpointRow
    from felix.db.session import tenant_session

    row = FileCheckpointRow
    per_digest = (
        select(func.max(row.size_bytes).label("size"))
        .where(row.tenant_id == tenant_id, row.before_state == "stored")
        .group_by(row.before_sha)
        .subquery()
    )
    async with tenant_session(settings, tenant_id) as db:
        total = await db.scalar(select(func.coalesce(func.sum(per_digest.c.size), 0)))
        return int(total or 0)


async def _still_named(
    settings: Settings, tenant_id: str, shas: set[str], thread_id: str, capture_ids: set[str]
) -> set[str]:
    """Of `shas`, those a capture of the tenant other than `capture_ids` (of `thread_id`) keeps."""
    if not shas:
        return set()
    if _use_memory(settings):
        return {
            r["before_sha"]
            for (t, th, c), r in _rows.items()
            if t == tenant_id
            and r["before_state"] == "stored"
            and r["before_sha"] in shas
            and not (th == thread_id and c in capture_ids)
        }
    from sqlalchemy import and_, not_, select

    from felix.db.models import FileCheckpointRow
    from felix.db.session import tenant_session

    row = FileCheckpointRow
    async with tenant_session(settings, tenant_id) as db:
        result = await db.execute(
            select(row.before_sha)
            .where(
                row.tenant_id == tenant_id,
                row.before_state == "stored",
                row.before_sha.in_(sorted(shas)),
                not_(and_(row.thread_id == thread_id, row.capture_id.in_(sorted(capture_ids)))),
            )
            .distinct()
        )
        return {str(sha) for (sha,) in result.all()}


async def drop_captures(
    settings: Settings, object_store: Any | None, tenant_id: str, thread_id: str, rows: list[dict[str, Any]]
) -> None:
    """Forget `rows` (all of `thread_id`): content no other capture names first, then the rows --
    bytes before rows, so drift is a row naming absent content, which a restore reports as skipped,
    never content no row names. One query for which content is still named, however many rows."""
    if not rows:
        return
    ids = {r["capture_id"] for r in rows}
    shas = {r["before_sha"] for r in rows if r["before_state"] == "stored" and r["before_sha"]}
    if object_store is not None:
        named = await _still_named(settings, tenant_id, shas, thread_id, ids)
        for sha in sorted(shas - named):
            await object_store.delete(checkpoint_key(tenant_id, sha))
    if _use_memory(settings):
        for capture_id in ids:
            _rows.pop((tenant_id, thread_id, capture_id), None)
        return
    from sqlalchemy import delete

    from felix.db.models import FileCheckpointRow
    from felix.db.session import tenant_session

    async with tenant_session(settings, tenant_id) as db:
        await db.execute(
            delete(FileCheckpointRow).where(
                FileCheckpointRow.tenant_id == tenant_id,
                FileCheckpointRow.thread_id == thread_id,
                FileCheckpointRow.capture_id.in_(sorted(ids)),
            )
        )
        await db.commit()


async def expired_paths(
    settings: Settings, *, older_than_ms: int, limit: int = 1000
) -> list[tuple[str, str, str]]:
    """`(tenant_id, thread_id, path)` whose newest capture in that thread is older than a cutoff,
    across tenants.

    A file's whole history in a thread at once: sweeping its earlier captures and keeping later
    ones would restore it to an in-between state and call it restored. And per file, not per
    thread: a file the agent deleted, and has not touched since, goes on time however busy the
    thread stays."""
    if _use_memory(settings):
        newest: dict[tuple[str, str, str], int] = {}
        for (t, th, _), r in _rows.items():
            key = (t, th, r["path"])
            newest[key] = max(newest.get(key, 0), int(r["created_at"]))
        old = [
            {"tenant_id": t, "thread_id": th, "path": path}
            for (t, th, path), ms in newest.items()
            if ms < older_than_ms
        ]
        old.sort(key=lambda r: (r["tenant_id"], r["thread_id"], r["path"]))
        return [(r["tenant_id"], r["thread_id"], r["path"]) for r in old][:limit]
    from sqlalchemy import func, select

    from felix.db.models import FileCheckpointRow
    from felix.db.session import get_session_factory, rls_bypass

    row = FileCheckpointRow
    factory = get_session_factory(settings=settings)
    with rls_bypass():
        async with factory() as db:
            result = await db.execute(
                select(row.tenant_id, row.thread_id, row.path)
                .group_by(row.tenant_id, row.thread_id, row.path)
                .having(func.max(row.created_at) < older_than_ms)
                .order_by(row.tenant_id, row.thread_id, row.path)
                .limit(limit)
            )
            return [(str(t), str(th), str(p)) for t, th, p in result.all()]


async def forget_path(
    settings: Settings,
    object_store: Any | None,
    tenant_id: str,
    thread_id: str,
    path: str,
    older_than_ms: int,
) -> int:
    """A file's captures in one thread, all older than the cutoff, and the content only they named;
    nothing when the file was changed again since `expired_paths` read it (its history stays whole).
    In batches, so a long history is never one statement. Returns the captures dropped."""
    dropped = 0
    while True:
        rows = await _path_rows(settings, tenant_id, thread_id, path)
        if not rows or any(int(r["created_at"]) >= older_than_ms for r in rows):
            return dropped
        await drop_captures(settings, object_store, tenant_id, thread_id, rows)
        dropped += len(rows)
        if len(rows) < MAX_RESTORE_CAPTURES:
            return dropped


async def _path_rows(settings: Settings, tenant_id: str, thread_id: str, path: str) -> list[dict[str, Any]]:
    """Up to `MAX_RESTORE_CAPTURES` of a file's captures in one thread, newest first, so a capture
    made since the sweep read the file is always among them."""
    if _use_memory(settings):
        rows = [
            dict(r)
            for (t, th, _), r in _rows.items()
            if t == tenant_id and th == thread_id and r["path"] == path
        ]
        return sorted(rows, key=lambda r: r["capture_id"], reverse=True)[:MAX_RESTORE_CAPTURES]
    from sqlalchemy import select

    from felix.db.models import FileCheckpointRow
    from felix.db.session import tenant_session

    row = FileCheckpointRow
    async with tenant_session(settings, tenant_id) as db:
        result = await db.execute(
            select(row)
            .where(row.tenant_id == tenant_id, row.thread_id == thread_id, row.path == path)
            .order_by(row.capture_id.desc())
            .limit(MAX_RESTORE_CAPTURES)
        )
        return [_row_dict(r) for r in result.scalars().all()]


async def forget_anchored(
    settings: Settings, object_store: Any | None, tenant_id: str, thread_id: str, anchors: Iterable[str]
) -> int:
    """Every capture of the thread anchored at `anchors`, and the content only they named: what a
    rewind does with the branch it abandons once it has (or has not) put its files back."""
    wanted = set(anchors)
    rows: list[dict[str, Any]] = []
    while batch := await captures(settings, tenant_id, thread_id, wanted, limit=MAX_RESTORE_CAPTURES):
        await drop_captures(settings, object_store, tenant_id, thread_id, batch)
        rows.extend(batch)
        if len(batch) < MAX_RESTORE_CAPTURES:
            break
    return len(rows)


__all__ = [
    "MAX_RESTORE_CAPTURES",
    "captures",
    "checkpoint_key",
    "clear_memory_ledger",
    "drop_captures",
    "expired_paths",
    "forget_anchored",
    "forget_path",
    "kept_bytes",
    "now_ms",
]
