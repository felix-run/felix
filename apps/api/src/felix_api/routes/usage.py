"""GET /usage — paginated token meter events."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
from felix.context import try_get_context

router = APIRouter(tags=["Usage"])


def _tenant(request: Request) -> str:
    ctx = try_get_context()
    if ctx is not None:
        return ctx.auth.tenant_id
    auth = getattr(request.state, "auth", None)
    return getattr(auth, "tenant_id", "default") if auth else "default"


@router.get("")
async def list_usage(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = None,
    manifest_id: str | None = None,
) -> dict[str, Any]:
    from felix.usage.store import query

    items, next_cursor = await query(
        request.app.state.settings,
        _tenant(request),
        limit=limit,
        cursor=cursor,
        manifest_id=manifest_id,
    )
    return {"items": items, "next_cursor": next_cursor}
