"""Turn-versioned memory store + consolidation."""

from __future__ import annotations

import time
import uuid
from typing import Any

from sqlalchemy import select, update

from felix.config import Settings
from felix.db.models import MemoryVector
from felix.db.session import _use_memory, get_session_factory


def now_ms() -> int:
    return int(time.time() * 1000)


_memory_rows: dict[tuple[str, str], dict[str, Any]] = {}


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
        "origin_seq": row.origin_seq,
        "superseded_seq": row.superseded_seq,
        "embedding_json": row.embedding_json,
    }


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
) -> dict[str, Any]:
    mem_id = uuid.uuid4().hex
    ts = now_ms()

    if supersedes_id:
        await supersede(settings, tenant_id, supersedes_id, origin_seq or 0)

    row = {
        "tenant_id": tenant_id,
        "id": mem_id,
        "kind": kind,
        "manifest_id": manifest_id,
        "content": content,
        "metadata": metadata or {},
        "created_at": ts,
        "origin_seq": origin_seq,
        "superseded_seq": None,
        "embedding_json": None,
    }
    if _use_memory(settings):
        _memory_rows[(tenant_id, mem_id)] = {
            **row,
            "metadata_json": row["metadata"],
        }
        return row

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        db.add(
            MemoryVector(
                tenant_id=tenant_id,
                id=mem_id,
                kind=kind,
                manifest_id=manifest_id,
                content=content,
                metadata_json=metadata or {},
                created_at=ts,
                origin_seq=origin_seq,
            )
        )
        await db.commit()
    return row


async def supersede(settings: Settings, tenant_id: str, memory_id: str, at_seq: int) -> None:
    if _use_memory(settings):
        row = _memory_rows.get((tenant_id, memory_id))
        if row is not None:
            row["superseded_seq"] = at_seq
        return
    factory = get_session_factory(settings=settings)
    async with factory() as db:
        await db.execute(
            update(MemoryVector)
            .where(MemoryVector.tenant_id == tenant_id, MemoryVector.id == memory_id)
            .values(superseded_seq=at_seq)
        )
        await db.commit()


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
            and r.get("superseded_seq") is None
            and (not manifest_id or r.get("manifest_id") == manifest_id)
            and (kind is None or r.get("kind") == kind)
        ]
        items.sort(key=lambda r: r["created_at"], reverse=True)
        return items[:limit]

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        stmt = select(MemoryVector).where(
            MemoryVector.tenant_id == tenant_id,
            MemoryVector.superseded_seq.is_(None),
        )
        if manifest_id:
            stmt = stmt.where(MemoryVector.manifest_id == manifest_id)
        if kind:
            stmt = stmt.where(MemoryVector.kind == kind)
        stmt = stmt.order_by(MemoryVector.created_at.desc()).limit(limit)
        return [_row_dict(r) for r in (await db.scalars(stmt)).all()]


async def consolidate_pools(settings: Settings, *, max_facts: int = 500) -> int:
    """Exact content-hash dedupe of active facts (not LLM summarization).

    ``max_facts`` caps how many active rows are scanned per pass (matches
    ``MemoryConsolidate.max_facts`` upper bound). Returns rows superseded.
    """
    scan_limit = max(1, min(int(max_facts), 5000))

    # Memory path
    if _use_memory(settings):
        seen: dict[tuple[str, str, str], str] = {}
        superseded = 0
        active = [
            ((tenant_id, mem_id), row)
            for (tenant_id, mem_id), row in _memory_rows.items()
            if row.get("superseded_seq") is None
        ]
        active.sort(key=lambda item: int(item[1].get("created_at") or 0))
        for (tenant_id, mem_id), row in active[:scan_limit]:
            key = (tenant_id, row.get("manifest_id", ""), row.get("content", ""))
            if key in seen:
                row["superseded_seq"] = row.get("origin_seq") or now_ms()
                superseded += 1
            else:
                seen[key] = mem_id
        return superseded

    # Postgres: scan active facts oldest-first within max_facts.
    factory = get_session_factory(settings=settings)
    superseded = 0
    async with factory() as db:
        rows = (
            await db.scalars(
                select(MemoryVector)
                .where(MemoryVector.superseded_seq.is_(None))
                .order_by(MemoryVector.created_at.asc())
                .limit(scan_limit)
            )
        ).all()
        seen: dict[tuple[str, str, str], str] = {}
        for row in rows:
            key = (row.tenant_id, row.manifest_id, row.content)
            if key in seen:
                row.superseded_seq = row.origin_seq or now_ms()
                superseded += 1
            else:
                seen[key] = row.id
        await db.commit()
    return superseded


__all__ = [
    "consolidate_pools",
    "list_active",
    "put_memory",
    "supersede",
]
