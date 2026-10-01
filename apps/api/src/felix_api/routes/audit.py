"""Audit event listing and export."""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from felix.auth.mgmt import SCOPE_AUDIT_READ, require_mgmt_scopes, tenant_id_from_request
from felix.cursors import InvalidCursor

from felix_api.errors import client_safe_message, internal_error_message

logger = logging.getLogger("felix_api.routes.audit")

router = APIRouter(tags=["Audit"])


def _time_range(
    since: int | None = Query(default=None, ge=0, description="Epoch ms, inclusive"),
    until: int | None = Query(default=None, ge=0, description="Epoch ms, exclusive"),
) -> tuple[int | None, int | None]:
    """The half-open `[since, until)` window both audit reads accept, refused when empty.

    A reversed range selects nothing, and an empty answer reads as "nothing happened".
    """
    if since is not None and until is not None and until <= since:
        raise HTTPException(status_code=400, detail="until must be later than since")
    return since, until


def _jsonl_line(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), default=str) + "\n"


@router.get("")
@router.get("/")
async def list_audit(
    request: Request,
    limit: int = Query(default=50, ge=1, le=500),
    cursor: str | None = None,
    event_type: str | None = None,
    status: str | None = None,
    manifest_id: str | None = None,
    time_range: tuple[int | None, int | None] = Depends(_time_range),
) -> dict[str, Any]:
    from felix.audit import store as audit_store

    require_mgmt_scopes(request, SCOPE_AUDIT_READ)
    since, until = time_range
    try:
        items, next_cursor = await audit_store.list_events(
            request.app.state.settings,
            tenant_id_from_request(request),
            limit=limit,
            cursor=cursor,
            event_type=event_type,
            status=status,
            manifest_id=manifest_id,
            since=since,
            until=until,
        )
    except InvalidCursor as exc:
        # A cursor is a query parameter, so it arrives from the client and can be anything.
        # Unhandled, a malformed one reached the caller as a 500 — a server error for what is
        # a bad request, and one that pages an operator for someone else's typo.
        #
        # `InvalidCursor`, not `ValueError`: the wider catch would report the next `ValueError`
        # the store grows — a JSON decode, a conversion — as a bad request, telling the client
        # something false and hiding a server bug. And the message is relayed through
        # `client_safe_message(authored_for_clients=True)` because `InvalidCursor` writes its
        # own; `errors.py` exists to keep an uncurated builtin's `str()` out of a response.
        raise HTTPException(
            status_code=400, detail=client_safe_message(exc, authored_for_clients=True)
        ) from exc
    # `events` alias keeps chat-ui clients that expect the TS shape working.
    return {"items": items, "events": items, "next_cursor": next_cursor}


# Rows per store read. The export streams, so this bounds memory, not the export's size.
_EXPORT_PAGE = 500


@router.get(
    "/export",
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "One audit event per line, newest first.",
            "content": {"application/x-ndjson": {"schema": {"type": "string"}}},
        }
    },
)
async def export_audit(
    request: Request,
    event_type: str | None = None,
    status: str | None = None,
    manifest_id: str | None = None,
    time_range: tuple[int | None, int | None] = Depends(_time_range),
) -> StreamingResponse:
    """Every matching event as JSONL, newest first — the whole range, not a page.

    Rows are shaped as `GET /audit` lists them, and nothing caps the count: an export that
    stopped at a limit would look complete to the auditor holding it. Only flushed events are
    read, so an export whose `until` is in the past is stable. The first page is read before
    the response starts, so a failure there is a status code rather than an empty file. One
    after it ends the file with an `{"error": ...}` line: raising instead leaves Granian holding
    the connection open, so the client hangs rather than seeing a truncated body.
    """
    from felix.audit import store as audit_store
    from felix.db.session import rls_tenant

    require_mgmt_scopes(request, SCOPE_AUDIT_READ)
    since, until = time_range
    settings = request.app.state.settings
    tenant_id = tenant_id_from_request(request)

    async def read_page(cursor: str | None) -> tuple[list[dict[str, Any]], str | None]:
        # Later pages are read while the body streams, after the handler has returned. The
        # middleware's binding still covers them today, since it wraps the whole ASGI call;
        # binding per read means the export does not depend on that staying true.
        with rls_tenant(tenant_id):
            return await audit_store.list_events(
                settings,
                tenant_id,
                limit=_EXPORT_PAGE,
                cursor=cursor,
                event_type=event_type,
                status=status,
                manifest_id=manifest_id,
                since=since,
                until=until,
            )

    first = await read_page(None)

    async def lines() -> AsyncIterator[str]:
        page, cursor = first
        try:
            while True:
                for event in page:
                    yield _jsonl_line(event)
                if cursor is None:
                    return
                page, cursor = await read_page(cursor)
        except Exception:  # cancellation is a BaseException and passes through
            # A store failure is never a relayable type, so the exception stays in the log.
            logger.exception("audit export failed after its first page")
            yield _jsonl_line({"error": "export_incomplete", "detail": internal_error_message()})

    # Built from integers only, so nothing a caller sends reaches the header.
    start = "start" if since is None else since
    end = "now" if until is None else until
    filename = f"audit-{start}-{end}.jsonl"
    return StreamingResponse(
        lines(),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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
