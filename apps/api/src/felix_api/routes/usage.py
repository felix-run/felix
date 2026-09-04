"""GET /usage — meter events with their cost; GET /usage/summary — spend by manifest, model, day."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from felix.auth.mgmt import SCOPE_USAGE_READ, require_mgmt_scopes, tenant_id_from_request

router = APIRouter(tags=["Usage"])


@router.get("")
async def list_usage(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = None,
    manifest_id: str | None = None,
) -> dict[str, Any]:
    from felix.usage.store import query

    require_mgmt_scopes(request, SCOPE_USAGE_READ)
    items, next_cursor = await query(
        request.app.state.settings,
        tenant_id_from_request(request),
        limit=limit,
        cursor=cursor,
        manifest_id=manifest_id,
    )
    return {"items": items, "next_cursor": next_cursor}


@router.get("/summary")
async def usage_summary(
    request: Request,
    since_ms: int | None = Query(
        None, ge=0, description="Inclusive lower bound, epoch ms. Default: 30 days ago."
    ),
    until_ms: int | None = Query(None, ge=0, description="Exclusive upper bound, epoch ms. Default: now."),
    manifest_id: str | None = None,
) -> dict[str, Any]:
    """Spend grouped by manifest, model and UTC day, with totals.

    Cost is what was priced at write time — by the wire model id and any
    `spec.model.price` override in force — so a later rate change does not rewrite history.
    """
    from felix.usage.store import summary

    require_mgmt_scopes(request, SCOPE_USAGE_READ)
    if since_ms is not None and until_ms is not None and since_ms >= until_ms:
        raise HTTPException(status_code=422, detail="since_ms must be before until_ms")
    return await summary(
        request.app.state.settings,
        tenant_id_from_request(request),
        since_ms=since_ms,
        until_ms=until_ms,
        manifest_id=manifest_id,
    )
