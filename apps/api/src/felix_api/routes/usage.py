"""GET /usage — meter events with their cost; GET /usage/summary — spend by manifest, model, day;
GET /usage/threads — spend by thread."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from felix.auth.mgmt import SCOPE_USAGE_READ, require_mgmt_scopes, tenant_id_from_request
from felix.cursors import InvalidCursor

from felix_api.errors import client_safe_message

router = APIRouter(tags=["Usage"])


@router.get("")
async def list_usage(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = None,
    manifest_id: str | None = None,
    thread_id: str | None = Query(
        default=None,
        description="The thread's suffix, as every chat route takes it; empty for calls outside a thread.",
    ),
) -> dict[str, Any]:
    """Usage rows, newest first, each with the cost it was priced at.

    `thread_id` narrows to one conversation and takes the **suffix** a client holds, composed
    into `{tenant}:{suffix}` here so a caller can never name another tenant's thread — the
    rule `/plans` follows. `?thread_id=` (empty) is a real value, the calls made outside any
    thread, distinct from omitting it; a suffix that cannot be a thread id answers 400
    `invalid_thread_id`. Each row's `thread_id` is the stored `{tenant}:{suffix}` form, the
    one the audit payload carries, and `''` for none.
    """
    from felix.usage.store import query

    require_mgmt_scopes(request, SCOPE_USAGE_READ)
    tenant_id = tenant_id_from_request(request)
    scoped = _scoped_thread(tenant_id, thread_id)
    try:
        items, next_cursor = await query(
            request.app.state.settings,
            tenant_id,
            limit=limit,
            cursor=cursor,
            manifest_id=manifest_id,
            thread_id=scoped,
        )
    except InvalidCursor as exc:
        # Same as `/audit`, including why the catch is narrow and the message is relayed.
        raise HTTPException(
            status_code=400, detail=client_safe_message(exc, authored_for_clients=True)
        ) from exc
    return {"items": items, "next_cursor": next_cursor}


def _scoped_thread(tenant_id: str, thread_id: str | None) -> str | None:
    if thread_id is None or thread_id == "":
        return thread_id
    from felix.thread_ids import effective_thread_id

    scoped = effective_thread_id(tenant_id, thread_id)
    if scoped is None:
        raise HTTPException(status_code=400, detail="invalid_thread_id")
    return scoped


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


@router.get("/threads")
async def usage_threads(
    request: Request,
    since_ms: int | None = Query(
        None, ge=0, description="Inclusive lower bound, epoch ms. Default: 30 days ago."
    ),
    until_ms: int | None = Query(None, ge=0, description="Exclusive upper bound, epoch ms. Default: now."),
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    """Spend grouped by thread over a window, most recently active first.

    Each item is one thread's calls, tokens, cost and first and last call in the window;
    `thread_id` is the stored `{tenant}:{suffix}` form, and the calls made outside any thread
    (the session summarizer's off-thread work, maintenance paths) are one item with
    `thread_id: ""`, ordered like any other. Items sort by `last_ts` descending, then
    `thread_id`. `totals` sums **every** thread in the window, not only the `limit` returned,
    and `truncated` is true when more threads exist than `limit`. The window is
    `/usage/summary`'s: half-open, thirty days by default.

    Rows written before the harness recorded a thread carry `''`, so a window reaching back
    past that upgrade attributes their spend to the no-thread item.
    """
    from felix.usage.store import threads

    require_mgmt_scopes(request, SCOPE_USAGE_READ)
    if since_ms is not None and until_ms is not None and since_ms >= until_ms:
        raise HTTPException(status_code=422, detail="since_ms must be before until_ms")
    return await threads(
        request.app.state.settings,
        tenant_id_from_request(request),
        since_ms=since_ms,
        until_ms=until_ms,
        limit=limit,
    )
