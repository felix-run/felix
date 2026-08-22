"""Audit event listing and export."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
from felix.context import try_get_context

router = APIRouter(tags=["Audit"])


def _tenant(request: Request) -> str:
    ctx = try_get_context()
    if ctx is not None:
        return ctx.auth.tenant_id
    auth = getattr(request.state, "auth", None)
    return getattr(auth, "tenant_id", "default") if auth else "default"


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

    items, next_cursor = await audit_store.list_events(
        request.app.state.settings,
        _tenant(request),
        limit=limit,
        cursor=cursor,
        event_type=event_type,
        status=status,
    )
    return {"items": items, "next_cursor": next_cursor}
