"""Audit event listing and export."""

from __future__ import annotations

import contextlib
from typing import Any

from fastapi import APIRouter, Query, Request
from felix.auth.mgmt import SCOPE_AUDIT_READ, require_mgmt_scopes, tenant_id_from_request

router = APIRouter(tags=["Audit"])


@router.get("")
@router.get("/")
async def list_audit(
    request: Request,
    limit: int = Query(default=50, ge=1, le=500),
    cursor: str | None = None,
    event_type: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    from felix.audit import store as audit_store

    require_mgmt_scopes(request, SCOPE_AUDIT_READ)
    items, next_cursor = await audit_store.list_events(
        request.app.state.settings,
        tenant_id_from_request(request),
        limit=limit,
        cursor=cursor,
        event_type=event_type,
        status=status,
    )
    # `events` alias keeps chat-ui clients that expect the TS shape working.
    return {"items": items, "events": items, "next_cursor": next_cursor}


@router.get("/metrics")
async def audit_metrics(
    request: Request,
    since: int | None = Query(default=None, description="Epoch ms lower bound"),
    limit: int = Query(default=200, ge=1, le=2000),
) -> dict[str, Any]:
    """Roll up recent ``tool_call`` audit rows for the inspector metrics panel."""
    from felix.audit import store as audit_store

    require_mgmt_scopes(request, SCOPE_AUDIT_READ)
    items, _ = await audit_store.list_events(
        request.app.state.settings,
        tenant_id_from_request(request),
        limit=limit,
        event_type="tool_call",
    )
    since_ms = since or 0
    tools: dict[str, dict[str, Any]] = {}
    for ev in items:
        if int(ev.get("ts") or 0) < since_ms:
            continue
        payload = ev.get("payload_json") or ev.get("payload") or {}
        name = str(payload.get("tool") or payload.get("name") or "unknown")
        row = tools.setdefault(
            name,
            {"tool": name, "calls": 0, "errors": 0, "latency_ms_sum": 0.0},
        )
        row["calls"] += 1
        status = str(ev.get("status") or payload.get("status") or "")
        if status in {"error", "failed"} or payload.get("error"):
            row["errors"] += 1
        latency = payload.get("latency_ms") or payload.get("duration_ms") or 0
        with contextlib.suppress(TypeError, ValueError):
            row["latency_ms_sum"] += float(latency)
    rollup = []
    for row in tools.values():
        calls = max(1, int(row["calls"]))
        rollup.append(
            {
                "tool": row["tool"],
                "calls": row["calls"],
                "errors": row["errors"],
                "avg_latency_ms": row["latency_ms_sum"] / calls,
            }
        )
    rollup.sort(key=lambda r: r["calls"], reverse=True)
    return {"tools": rollup, "window_since": since_ms}
