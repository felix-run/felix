"""Audit event store — buffered writes and paginated reads."""

from __future__ import annotations

import time
import uuid
from typing import Any

from sqlalchemy import select

from felix.config import Settings
from felix.db.models import AuditEvent
from felix.db.session import _use_memory, get_session_factory

now_ms = lambda: int(time.time() * 1000)

_pending: list[dict[str, Any]] = []
_memory_events: list[dict[str, Any]] = []


def _event_dict(row: AuditEvent | dict[str, Any]) -> dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    return {
        "id": row.id,
        "tenant_id": row.tenant_id,
        "ts": row.ts,
        "event_type": row.event_type,
        "manifest_id": row.manifest_id,
        "principal_subj": row.principal_subj,
        "status": row.status,
        "payload_json": row.payload_json,
    }


def record_event(
    settings: Settings,
    tenant_id: str,
    event_type: str,
    **fields: Any,
) -> None:
    """Buffer an audit event for later flush."""
    from felix.secrets import redact_json

    _ = settings
    payload = fields.get("payload_json") or fields.get("payload") or {}
    event = {
        "tenant_id": tenant_id,
        "id": fields.get("id") or uuid.uuid4().hex,
        "ts": fields.get("ts", now_ms()),
        "event_type": event_type,
        "manifest_id": fields.get("manifest_id", ""),
        "principal_subj": fields.get("principal_subj", ""),
        "status": fields.get("status", ""),
        "payload_json": redact_json(payload) if payload else {},
    }
    _pending.append(event)


async def query(
    settings: Settings,
    tenant_id: str,
    *,
    limit: int = 50,
    cursor: str | None = None,
    event_type: str | None = None,
    status: str | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Query audit events for a tenant."""
    if _use_memory(settings):
        items = [e for e in _memory_events if e["tenant_id"] == tenant_id]
        if event_type is not None:
            items = [e for e in items if e["event_type"] == event_type]
        if status is not None:
            items = [e for e in items if e["status"] == status]
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
            select(AuditEvent)
            .where(AuditEvent.tenant_id == tenant_id)
            .order_by(AuditEvent.ts.desc())
            .limit(limit + 1)
        )
        if event_type is not None:
            stmt = stmt.where(AuditEvent.event_type == event_type)
        if status is not None:
            stmt = stmt.where(AuditEvent.status == status)
        if cursor is not None:
            stmt = stmt.where(AuditEvent.ts < int(cursor))
        rows = (await db.scalars(stmt)).all()
        page = rows[:limit]
        next_cursor = str(page[-1].ts) if len(rows) > limit else None
        return [_event_dict(r) for r in page], next_cursor


async def list_events(
    settings: Settings,
    tenant_id: str,
    *,
    limit: int = 50,
    cursor: str | None = None,
    event_type: str | None = None,
    status: str | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Paginated audit listing used by API routes."""
    return await query(
        settings,
        tenant_id,
        limit=limit,
        cursor=cursor,
        event_type=event_type,
        status=status,
    )


async def flush_pending(settings: Settings) -> int:
    """Drain buffered audit events to Postgres, then optional warehouse spill."""
    if not _pending:
        return 0

    batch = list(_pending)
    _pending.clear()

    if _use_memory(settings):
        _memory_events.extend(batch)
    else:
        factory = get_session_factory(settings=settings)
        async with factory() as db:
            for event in batch:
                db.add(
                    AuditEvent(
                        tenant_id=event["tenant_id"],
                        id=event["id"],
                        ts=event["ts"],
                        event_type=event["event_type"],
                        manifest_id=event.get("manifest_id", ""),
                        principal_subj=event.get("principal_subj", ""),
                        status=event.get("status", ""),
                        payload_json=event.get("payload_json") or {},
                    )
                )
            await db.commit()

    if getattr(settings, "warehouse", "none") not in {"none", "", None}:
        from felix.warehouse import export_audit_events

        # Map payload_json → payload for warehouse row shape.
        spill = [
            {
                **e,
                "payload": e.get("payload_json") or e.get("payload") or {},
            }
            for e in batch
        ]
        await export_audit_events(settings, spill)

    return len(batch)


__all__ = ["flush_pending", "list_events", "query", "record_event"]
