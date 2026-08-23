"""Long-term memory rows: content-addressed, superseded rather than deleted.

Two things make a memory row more than a log line.

**Content-addressed ids.** The id is a hash of the content, scoped by manifest, so
storing the same fact twice collapses instead of accumulating near-duplicates. The
manifest is part of the hash on purpose: the primary key is `(tenant_id, id)`, so
hashing content alone would collide two manifests in one tenant onto one row.

**Supersession, along two axes that must agree.** `status` is current state and is
the only one that can express `forgotten`, which has no position in turn time.
`superseded_seq` closes the row's validity interval in turn time, which is what lets
`as_of` reconstruct what was known at turn N — including facts later replaced, which
a query over `status='active'` cannot do. Every write that closes a memory sets both
in the same statement.
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any, cast

from sqlalchemy import func, select, update

from felix.config import Settings
from felix.db.models import MemoryVector
from felix.db.session import _use_memory, get_session_factory

logger = logging.getLogger("felix.memory")

ACTIVE = "active"
SUPERSEDED = "superseded"
FORGOTTEN = "forgotten"


def now_ms() -> int:
    return int(time.time() * 1000)


_memory_rows: dict[tuple[str, str], dict[str, Any]] = {}


def memory_id(manifest_id: str, content: str) -> str:
    """A stable id for this content under this manifest.

    Whitespace-normalised and lowercased so trivially different renderings of the same
    sentence land on the same row.
    """
    normalized = " ".join((content or "").lower().split())
    return hashlib.sha256(f"{manifest_id}\x00{normalized}".encode()).hexdigest()[:32]


def _row_dict(row: MemoryVector | dict[str, Any]) -> dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    return {
        "tenant_id": row.tenant_id,
        "id": row.id,
        "kind": row.kind,
        "manifest_id": row.manifest_id,
        "content": row.content,
        "metadata": row.metadata_json,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "last_used_at": row.last_used_at,
        "thread_id": row.thread_id,
        "topic_key": row.topic_key,
        "status": row.status,
        "superseded_by": row.superseded_by,
        "importance": row.importance,
        "origin_seq": row.origin_seq,
        "superseded_seq": row.superseded_seq,
        "embedding_dim": row.embedding_dim,
        "embedding_model": row.embedding_model,
        "embedding_json": row.embedding_json,
    }


def _is_active(row: dict[str, Any]) -> bool:
    return str(row.get("status") or ACTIVE) == ACTIVE


async def current_turn_seq(settings: Settings, tenant_id: str, *, manifest_id: str = "") -> int:
    """The highest turn ordinal any memory in this scope was written at (0 if none)."""
    if _use_memory(settings):
        seqs = [
            int(r.get("origin_seq") or 0)
            for (t, _), r in _memory_rows.items()
            if t == tenant_id and (not manifest_id or r.get("manifest_id") == manifest_id)
        ]
        return max(seqs, default=0)

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        stmt = select(func.coalesce(func.max(MemoryVector.origin_seq), 0)).where(
            MemoryVector.tenant_id == tenant_id
        )
        if manifest_id:
            stmt = stmt.where(MemoryVector.manifest_id == manifest_id)
        return int(await db.scalar(stmt) or 0)


async def put_memory(
    settings: Settings,
    tenant_id: str,
    *,
    content: str,
    kind: str = "fact",
    manifest_id: str = "",
    origin_seq: int | None = None,
    metadata: dict[str, Any] | None = None,
    supersedes_id: str | None = None,
    topic_key: str | None = None,
    importance: float = 0.5,
    thread_id: str = "",
    embedding: list[float] | None = None,
    embedding_model: str = "",
) -> dict[str, Any]:
    """Store a memory, superseding any active row that shares its ``topic_key``.

    Idempotent by content: re-storing the same text under the same manifest reactivates
    the existing row and keeps its original provenance, rather than adding a second
    copy that recall would then return twice.

    Wide by design — a memory is a wide record, and every field past ``content`` is
    keyword-only with a default, so callers name what they mean.
    """
    mem_id = memory_id(manifest_id, content)
    ts = now_ms()
    row: dict[str, Any] = {
        "tenant_id": tenant_id,
        "id": mem_id,
        "kind": kind,
        "manifest_id": manifest_id,
        "content": content,
        "metadata": metadata or {},
        "created_at": ts,
        "updated_at": ts,
        "last_used_at": None,
        "thread_id": thread_id,
        "topic_key": topic_key,
        "status": ACTIVE,
        "superseded_by": None,
        "importance": max(0.0, min(float(importance), 1.0)),
        "origin_seq": origin_seq,
        "superseded_seq": None,
        "embedding_dim": len(embedding) if embedding else None,
        "embedding_model": embedding_model,
        "embedding_json": None,
    }

    if supersedes_id:
        # The ordinal a supersession closes at is this turn's, not the old row's.
        await supersede(settings, tenant_id, supersedes_id, origin_seq, superseded_by=mem_id)

    if _use_memory(settings):
        # The twin keeps the vector inline; Postgres keeps it in a column the ORM
        # cannot see. Either way recall must be able to reach it, or the vector
        # channel is untestable on the path CI actually runs.
        row["embedding"] = list(embedding) if embedding else None
        return _put_in_memory(row)
    await _put_in_postgres(settings, row, embedding=embedding)
    return row


def _put_in_memory(row: dict[str, Any]) -> dict[str, Any]:
    tenant_id, mem_id = row["tenant_id"], row["id"]
    if row["topic_key"]:
        for (other_tenant, other_id), other in _memory_rows.items():
            if (
                other_tenant == tenant_id
                and other_id != mem_id
                and other.get("manifest_id") == row["manifest_id"]
                and other.get("topic_key") == row["topic_key"]
                and _is_active(other)
            ):
                other["status"] = SUPERSEDED
                other["superseded_by"] = mem_id
                other["superseded_seq"] = row["origin_seq"]
                other["updated_at"] = row["updated_at"]

    existing = _memory_rows.get((tenant_id, mem_id))
    if existing is not None:
        # Reactivating keeps the first write's provenance; a later one only fills a gap.
        row["created_at"] = existing.get("created_at", row["created_at"])
        if existing.get("origin_seq") is not None:
            row["origin_seq"] = existing["origin_seq"]
    _memory_rows[(tenant_id, mem_id)] = {**row, "metadata_json": row["metadata"]}
    return row


async def _put_in_postgres(settings: Settings, row: dict[str, Any], *, embedding: list[float] | None) -> None:
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    tenant_id, mem_id = row["tenant_id"], row["id"]
    factory = get_session_factory(settings=settings)
    async with factory() as db:
        # Supersession and insert share one transaction: a crash between them would
        # otherwise leave a topic with two active rows, or none.
        if row["topic_key"]:
            await db.execute(
                update(MemoryVector)
                .where(
                    MemoryVector.tenant_id == tenant_id,
                    MemoryVector.manifest_id == row["manifest_id"],
                    MemoryVector.topic_key == row["topic_key"],
                    MemoryVector.status == ACTIVE,
                    MemoryVector.id != mem_id,
                )
                .values(
                    status=SUPERSEDED,
                    superseded_by=mem_id,
                    superseded_seq=row["origin_seq"],
                    updated_at=row["updated_at"],
                )
            )

        # Target the Table, not the ORM class, and address columns by name. The
        # column is `metadata`, but `MemoryVector.metadata` is SQLAlchemy's own
        # MetaData object, so any attribute-based resolution of that key finds the
        # wrong thing and fails deep inside the insert path.
        # `__table__` is declared as `FromClause` but is a `Table` at runtime, and
        # `pg_insert` wants the narrower type; cast rather than lie in an annotation.
        table = cast(Any, MemoryVector.__table__)
        values = {k: v for k, v in row.items() if k not in {"last_used_at", "embedding_json", "embedding"}}
        stmt = pg_insert(table).values(values)
        excluded = stmt.excluded
        stmt = stmt.on_conflict_do_update(
            index_elements=["tenant_id", "id"],
            set_={
                "kind": excluded["kind"],
                "content": excluded["content"],
                "metadata": excluded["metadata"],
                "topic_key": excluded["topic_key"],
                "importance": excluded["importance"],
                "thread_id": excluded["thread_id"],
                "status": ACTIVE,
                "superseded_by": None,
                "superseded_seq": None,
                "updated_at": excluded["updated_at"],
                # Re-remembering reactivates a row; it does not rewrite its history,
                # so the first write's provenance wins and a later one only fills a gap.
                "origin_seq": func.coalesce(table.c.origin_seq, excluded["origin_seq"]),
            },
        )
        await db.execute(stmt)
        if embedding:
            await _write_embedding(db, tenant_id, mem_id, embedding, model=row["embedding_model"])
        await db.commit()


_warned_embedding_dim = False


async def _write_embedding(db: Any, tenant_id: str, mem_id: str, vector: list[float], *, model: str) -> None:
    """Set the pgvector column, which has no ORM representation.

    A vector whose length does not match the column is rejected loudly. Postgres would
    reject it anyway, and the previous behaviour — swallow the error into a debug log —
    is how this table came to look like it worked while storing nothing: a misconfigured
    embedder would produce a memory store with no vectors and no indication why.
    """
    global _warned_embedding_dim
    from sqlalchemy import text as sa_text

    expected = await _configured_dim(db)
    if expected is not None and len(vector) != expected:
        if not _warned_embedding_dim:
            _warned_embedding_dim = True
            logger.warning(
                "embedding is %d-dimensional but memory_vectors.embedding is vector(%d); "
                "vectors are not being stored and recall will run without them. Set "
                "FELIX_MEMORY_EMBEDDING_MODEL to a model of the right size, or rebuild "
                "the column.",
                len(vector),
                expected,
            )
        return

    literal = "[" + ",".join(repr(float(v)) for v in vector) + "]"
    await db.execute(
        sa_text(
            "UPDATE memory_vectors SET embedding = CAST(:vec AS vector), "
            "embedding_dim = :dim, embedding_model = :model "
            "WHERE tenant_id = :tenant AND id = :id"
        ),
        {"vec": literal, "dim": len(vector), "model": model, "tenant": tenant_id, "id": mem_id},
    )


async def _configured_dim(db: Any) -> int | None:
    """The dimension the vector column was built at, per `memory_vector_config`."""
    from sqlalchemy import text as sa_text

    try:
        return await db.scalar(sa_text("SELECT dim FROM memory_vector_config WHERE id = 1"))
    except Exception:
        logger.debug("memory_vector_config unreadable", exc_info=True)
        return None


async def supersede(
    settings: Settings,
    tenant_id: str,
    memory_id: str,
    at_seq: int | None,
    *,
    superseded_by: str | None = None,
) -> None:
    """Close a memory's validity interval at ``at_seq``, on both axes."""
    ts = now_ms()
    if _use_memory(settings):
        row = _memory_rows.get((tenant_id, memory_id))
        if row is not None:
            row["status"] = SUPERSEDED
            row["superseded_seq"] = at_seq
            row["superseded_by"] = superseded_by
            row["updated_at"] = ts
        return
    factory = get_session_factory(settings=settings)
    async with factory() as db:
        await db.execute(
            update(MemoryVector)
            .where(MemoryVector.tenant_id == tenant_id, MemoryVector.id == memory_id)
            .values(
                status=SUPERSEDED,
                superseded_seq=at_seq,
                superseded_by=superseded_by,
                updated_at=ts,
            )
        )
        await db.commit()


async def forget(settings: Settings, tenant_id: str, memory_id: str) -> bool:
    """Hide a memory from recall without deleting it.

    `forgotten` has no turn-time endpoint on purpose: it is an out-of-band decision by
    an operator, not something a turn did, so it must not appear as a supersession in
    an as-of reconstruction.
    """
    ts = now_ms()
    if _use_memory(settings):
        row = _memory_rows.get((tenant_id, memory_id))
        if row is None:
            return False
        row["status"] = FORGOTTEN
        row["updated_at"] = ts
        return True
    factory = get_session_factory(settings=settings)
    async with factory() as db:
        result = await db.execute(
            update(MemoryVector)
            .where(MemoryVector.tenant_id == tenant_id, MemoryVector.id == memory_id)
            .values(status=FORGOTTEN, updated_at=ts)
        )
        await db.commit()
        # `execute` is typed as returning `Result`, but an UPDATE yields a
        # `CursorResult`, which is where `rowcount` lives.
        return bool(getattr(result, "rowcount", 0))


async def get_many(settings: Settings, tenant_id: str, ids: list[str]) -> dict[str, dict[str, Any]]:
    """Resolve many memories in one query — recall fuses candidate ids before reading."""
    if not ids:
        return {}
    if _use_memory(settings):
        return {
            mem_id: _row_dict(row)
            for mem_id in ids
            if (row := _memory_rows.get((tenant_id, mem_id))) is not None
        }
    factory = get_session_factory(settings=settings)
    async with factory() as db:
        rows = (
            await db.scalars(
                select(MemoryVector).where(MemoryVector.tenant_id == tenant_id, MemoryVector.id.in_(ids))
            )
        ).all()
        return {row.id: _row_dict(row) for row in rows}


async def list_active(
    settings: Settings,
    tenant_id: str,
    *,
    manifest_id: str = "",
    kind: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    if _use_memory(settings):
        items = [
            _row_dict(r)
            for (t, _), r in _memory_rows.items()
            if t == tenant_id
            and _is_active(r)
            and (not manifest_id or r.get("manifest_id") == manifest_id)
            and (kind is None or r.get("kind") == kind)
        ]
        items.sort(key=lambda r: r["created_at"], reverse=True)
        return items[:limit]

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        stmt = select(MemoryVector).where(
            MemoryVector.tenant_id == tenant_id,
            MemoryVector.status == ACTIVE,
        )
        if manifest_id:
            stmt = stmt.where(MemoryVector.manifest_id == manifest_id)
        if kind:
            stmt = stmt.where(MemoryVector.kind == kind)
        stmt = stmt.order_by(MemoryVector.created_at.desc()).limit(limit)
        return [_row_dict(r) for r in (await db.scalars(stmt)).all()]


async def as_of(
    settings: Settings,
    tenant_id: str,
    turn_seq: int,
    *,
    manifest_id: str = "",
    kind: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """The memories that were current at turn ``turn_seq``.

    Includes facts that were later superseded, which is the whole point — a query over
    `status='active'` shows what is believed now, not what was believed then. Rows that
    predate provenance have a null `origin_seq` and read as genesis, so they appear in
    every as-of view.
    """
    if _use_memory(settings):
        items = [
            _row_dict(r)
            for (t, _), r in _memory_rows.items()
            if t == tenant_id
            and (not manifest_id or r.get("manifest_id") == manifest_id)
            and (kind is None or r.get("kind") == kind)
            and int(r.get("origin_seq") or 0) <= turn_seq
            and (r.get("superseded_seq") is None or int(r["superseded_seq"]) > turn_seq)
        ]
        items.sort(key=lambda r: r["created_at"], reverse=True)
        return items[:limit]

    from sqlalchemy import or_

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        stmt = select(MemoryVector).where(
            MemoryVector.tenant_id == tenant_id,
            func.coalesce(MemoryVector.origin_seq, 0) <= turn_seq,
            or_(MemoryVector.superseded_seq.is_(None), MemoryVector.superseded_seq > turn_seq),
        )
        if manifest_id:
            stmt = stmt.where(MemoryVector.manifest_id == manifest_id)
        if kind:
            stmt = stmt.where(MemoryVector.kind == kind)
        stmt = stmt.order_by(MemoryVector.created_at.desc()).limit(limit)
        return [_row_dict(r) for r in (await db.scalars(stmt)).all()]


async def consolidate_pools(settings: Settings, *, max_facts: int = 500) -> int:
    """Exact content-hash dedupe of active facts (not LLM summarization).

    Largely vestigial now that ids are content hashes — a duplicate collapses on write
    rather than accumulating — but it still cleans up rows written before that, and
    rows whose text differs only by whitespace or case.

    ``max_facts`` caps how many active rows are scanned per pass. Returns rows
    superseded.
    """
    scan_limit = max(1, min(int(max_facts), 5000))

    if _use_memory(settings):
        seen: dict[tuple[str, str, str], str] = {}
        superseded = 0
        active = [
            ((tenant_id, mem_id), row) for (tenant_id, mem_id), row in _memory_rows.items() if _is_active(row)
        ]
        active.sort(key=lambda item: int(item[1].get("created_at") or 0))
        for (tenant_id, mem_id), row in active[:scan_limit]:
            key = (tenant_id, row.get("manifest_id", ""), row.get("content", ""))
            if key in seen:
                row["status"] = SUPERSEDED
                row["superseded_by"] = seen[key]
                # Deliberately NOT a timestamp. This column is a turn ordinal, and
                # writing now_ms() into it — as this did — makes every later as-of
                # comparison wrong by thirteen orders of magnitude.
                row["superseded_seq"] = row.get("origin_seq")
                superseded += 1
            else:
                seen[key] = mem_id
        return superseded

    # Cross-tenant sweep, so it must bypass RLS the way the retention job does —
    # without this the worker cron silently sees zero rows under FELIX_DATABASE_RLS.
    from felix.db.session import rls_bypass

    factory = get_session_factory(settings=settings)
    superseded = 0
    with rls_bypass():
        async with factory() as db:
            rows = (
                await db.scalars(
                    select(MemoryVector)
                    .where(MemoryVector.status == ACTIVE)
                    .order_by(MemoryVector.created_at.asc())
                    .limit(scan_limit)
                )
            ).all()
            seen_pg: dict[tuple[str, str, str], str] = {}
            for row in rows:
                key = (row.tenant_id, row.manifest_id, row.content)
                if key in seen_pg:
                    row.status = SUPERSEDED
                    row.superseded_by = seen_pg[key]
                    row.superseded_seq = row.origin_seq
                    row.updated_at = now_ms()
                    superseded += 1
                else:
                    seen_pg[key] = row.id
            await db.commit()
    return superseded


__all__ = [
    "ACTIVE",
    "FORGOTTEN",
    "SUPERSEDED",
    "as_of",
    "consolidate_pools",
    "current_turn_seq",
    "forget",
    "get_many",
    "list_active",
    "memory_id",
    "put_memory",
    "supersede",
]
