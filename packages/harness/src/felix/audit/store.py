"""Audit event store — buffered writes and paginated reads."""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from sqlalchemy import select

from felix.buffers import DurableBuffer
from felix.config import Settings
from felix.db.models import AuditEvent
from felix.db.session import _use_memory, get_session_factory

logger = logging.getLogger("felix.audit.store")

now_ms = lambda: int(time.time() * 1000)

_pending = DurableBuffer("audit")
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
    _fanout_to_plugin_sink(event)


def _fanout_to_plugin_sink(event: dict[str, Any]) -> None:
    """Mirror the event to a plugin audit sink, if one is registered.

    Postgres stays the system of record; a sink is an additional consumer (SIEM,
    compliance export). It must never be able to lose an audit event, so failure
    is logged and swallowed — same contract as the usage sink.
    """
    try:
        from felix.plugins import get_registry

        sink = get_registry().audit_sink()
        if sink is None:
            return
        record = getattr(sink, "record", None)
        if callable(record):
            record(event)
    except Exception:
        # Warning, not debug: an audit event that cannot be exported is a
        # governance gap, and this is the SIEM/compliance path. Still swallowed —
        # Postgres is the system of record and must not be blocked by a sink.
        logger.warning("audit_sink_failed", exc_info=True)


async def list_tenants_with_events(settings: Settings) -> list[str]:
    """Every tenant that has at least one audit event.

    `run_anomaly_scan` defaulted to ``tenant_id="default"`` and the worker cron never
    passed one, so anomaly detection covered a single tenant. Same shape as
    `jobs.store.list_tenants_with_jobs`, including the bypass.
    """
    if _use_memory(settings):
        return sorted({str(e["tenant_id"]) for e in _memory_events})

    from felix.db.session import rls_bypass

    factory = get_session_factory(settings=settings)
    # Cross-tenant maintenance: without a bypass the sweep runs with no
    # app.tenant_id GUC and RLS returns nothing for everyone.
    with rls_bypass():
        async with factory() as db:
            rows = (await db.execute(select(AuditEvent.tenant_id).distinct())).scalars().all()
            return sorted({str(r) for r in rows})


async def list_manifests_with_events(settings: Settings) -> list[tuple[str, str]]:
    """Every ``(tenant_id, manifest_id)`` pair with at least one audit event.

    What retention resolves a manifest for: the pairs that have rows, rather than the
    manifests a store lists — a manifest served from the object store or the image has
    rows too, and only the resolver knows which document governs a pair.
    """
    if _use_memory(settings):
        return sorted({(str(e["tenant_id"]), str(e.get("manifest_id") or "")) for e in _memory_events})

    from felix.db.session import rls_bypass

    factory = get_session_factory(settings=settings)
    with rls_bypass():
        async with factory() as db:
            rows = (await db.execute(select(AuditEvent.tenant_id, AuditEvent.manifest_id).distinct())).all()
            return sorted({(str(t), str(m or "")) for t, m in rows})


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
    batch = _pending.take()
    if not batch:
        return 0

    try:
        await _write_batch(settings, batch)
    except Exception:
        # Never drop the compliance record because a commit failed — put it back and
        # let the next flush retry.
        _pending.requeue(batch)
        raise
    return len(batch)


async def _write_batch(settings: Settings, batch: list[dict[str, Any]]) -> None:
    if _use_memory(settings):
        _memory_events.extend(batch)
    else:
        from collections import defaultdict

        from felix.db.session import apply_tenant_rls, rls_tenant

        by_tenant: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for event in batch:
            by_tenant[str(event.get("tenant_id") or "default")].append(event)

        factory = get_session_factory(settings=settings)
        for tenant_id, events in by_tenant.items():
            with rls_tenant(tenant_id):
                async with factory() as db:
                    await apply_tenant_rls(db, settings, tenant_id)
                    for event in events:
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


__all__ = [
    "flush_pending",
    "list_events",
    "list_manifests_with_events",
    "list_tenants_with_events",
    "pending_buffer",
    "query",
    "record_event",
]


def pending_buffer() -> DurableBuffer:
    """The process-local audit buffer (diagnostics, metrics, tests)."""
    return _pending
