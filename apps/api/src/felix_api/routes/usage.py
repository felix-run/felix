"""GET /usage — paginated token meter events."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
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
