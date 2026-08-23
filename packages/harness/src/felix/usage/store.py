"""Usage meters — buffered token/turn events flushed like audit."""

from __future__ import annotations

import time
import uuid
from typing import Any

from sqlalchemy import select

from felix.buffers import DurableBuffer
from felix.config import Settings
from felix.db.models import UsageEvent
from felix.db.session import _use_memory, get_session_factory


def now_ms() -> int:
    return int(time.time() * 1000)


_pending = DurableBuffer("usage")
_memory_events: list[dict[str, Any]] = []


def _event_dict(row: UsageEvent | dict[str, Any]) -> dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    return {
        "id": row.id,
        "tenant_id": row.tenant_id,
        "ts": row.ts,
        "manifest_id": row.manifest_id,
        "model_id": row.model_id,
        "kind": row.kind,
        "tokens_input": row.tokens_input,
        "tokens_output": row.tokens_output,
        "cache_creation": row.cache_creation,
        "cache_read": row.cache_read,
        "meta_json": row.meta_json,
    }


def record_tokens(
    settings: Settings,
    *,
    tenant_id: str,
    manifest_id: str,
    model_id: str = "",
    tokens_input: int = 0,
    tokens_output: int = 0,
    cache_creation: int = 0,
    cache_read: int = 0,
    meta: dict[str, Any] | None = None,
) -> None:
    """Buffer a token-usage event for later flush."""
    _ = settings
    event = {
        "id": uuid.uuid4().hex,
        "tenant_id": tenant_id or "default",
        "ts": now_ms(),
        "manifest_id": manifest_id or "",
        "model_id": model_id or "",
        "kind": "tokens",
        "tokens_input": int(tokens_input or 0),
        "tokens_output": int(tokens_output or 0),
        "cache_creation": int(cache_creation or 0),
        "cache_read": int(cache_read or 0),
        "meta_json": meta or {},
    }
    _pending.append(event)


async def query(
    settings: Settings,
    tenant_id: str,
    *,
    limit: int = 50,
    cursor: str | None = None,
    manifest_id: str | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    if _use_memory(settings):
        items = [e for e in _memory_events if e["tenant_id"] == tenant_id]
        if manifest_id is not None:
            items = [e for e in items if e["manifest_id"] == manifest_id]
        if cursor is not None:
            cursor_ts = int(cursor)
            items = [e for e in items if e["ts"] < cursor_ts]
        items.sort(key=lambda e: e["ts"], reverse=True)
        page = items[:limit]
        next_cursor = str(page[-1]["ts"]) if len(items) > limit and page else None
        return [_event_dict(e) for e in page], next_cursor

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        stmt = (
            select(UsageEvent)
            .where(UsageEvent.tenant_id == tenant_id)
            .order_by(UsageEvent.ts.desc())
            .limit(limit + 1)
        )
        if manifest_id is not None:
            stmt = stmt.where(UsageEvent.manifest_id == manifest_id)
        if cursor is not None:
            stmt = stmt.where(UsageEvent.ts < int(cursor))
        rows = (await db.scalars(stmt)).all()
        page = rows[:limit]
        next_cursor = str(page[-1].ts) if len(rows) > limit else None
        return [_event_dict(r) for r in page], next_cursor


async def flush_pending(settings: Settings) -> int:
    """Drain buffered usage events to Postgres (or memory)."""
    batch = _pending.take()
    if not batch:
        return 0

    try:
        await _write_batch(settings, batch)
    except Exception:
        # Usage drives billing — a failed commit must not silently lose the meter.
        _pending.requeue(batch)
        raise
    return len(batch)


async def _write_batch(settings: Settings, batch: list[dict[str, Any]]) -> None:
    if _use_memory(settings):
        _memory_events.extend(batch)
        return

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        for event in batch:
            db.add(
                UsageEvent(
                    tenant_id=event["tenant_id"],
                    id=event["id"],
                    ts=event["ts"],
                    manifest_id=event.get("manifest_id", ""),
                    model_id=event.get("model_id", ""),
                    kind=event.get("kind", "tokens"),
                    tokens_input=int(event.get("tokens_input") or 0),
                    tokens_output=int(event.get("tokens_output") or 0),
                    cache_creation=int(event.get("cache_creation") or 0),
                    cache_read=int(event.get("cache_read") or 0),
                    meta_json=event.get("meta_json") or {},
                )
            )
        await db.commit()


def pending_count() -> int:
    return len(_pending)


def pending_buffer() -> DurableBuffer:
    """The process-local usage buffer (diagnostics, metrics, tests)."""
    return _pending


def clear_memory() -> None:
    """Test helper."""
    _pending.reset_for_tests()
    _memory_events.clear()


__all__ = [
    "clear_memory",
    "flush_pending",
    "pending_buffer",
    "pending_count",
    "query",
    "record_tokens",
]
