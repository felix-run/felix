"""Durable plan store (agent-authored multi-step plans)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from felix.auth.mgmt import (
    SCOPE_PLANS_READ,
    SCOPE_PLANS_WRITE,
    require_mgmt_scopes,
    tenant_id_from_request,
)
from pydantic import BaseModel

router = APIRouter(tags=["Plans"])


class PlanUpsert(BaseModel):
    model_config = {"extra": "forbid"}

    plan: dict[str, Any]
    # Omitted means "leave as stored" (a new plan gets "" / no expiry). Sending a
    # value, including `expires_at: null`, sets it.
    manifest_id: str = ""
    expires_at: int | None = None
    # The `updated_at` this edit was based on. When set, the write succeeds only if
    # the plan is still exactly that version, and answers 409 with the current row
    # otherwise — the agent updates step status while a run is live, and without
    # this an operator's save and the agent's update erase each other silently.
    expected_updated_at: int | None = None


@router.get("")
@router.get("/")
async def list_plans(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    from felix.plans import store as plans_store

    require_mgmt_scopes(request, SCOPE_PLANS_READ)
    items = await plans_store.list_plans(
        request.app.state.settings, tenant_id_from_request(request), limit=limit
    )
    return {"items": items, "plans": items}


@router.get("/{plan_id}")
async def get_plan(plan_id: str, request: Request) -> Any:
    from felix.plans import store as plans_store

    require_mgmt_scopes(request, SCOPE_PLANS_READ)
    row = await plans_store.get_plan(request.app.state.settings, tenant_id_from_request(request), plan_id)
    if row is None:
        raise HTTPException(status_code=404, detail="not_found")
    return row


@router.put("/{plan_id}")
async def upsert_plan(plan_id: str, body: PlanUpsert, request: Request) -> Any:
    from felix.plans import store as plans_store

    require_mgmt_scopes(request, SCOPE_PLANS_WRITE)
    sent = body.model_fields_set
    try:
        return await plans_store.put_plan(
            request.app.state.settings,
            tenant_id_from_request(request),
            plan_id,
            plan=body.plan,
            manifest_id=body.manifest_id if "manifest_id" in sent else plans_store.KEEP,
            expires_at=body.expires_at if "expires_at" in sent else plans_store.KEEP,
            expected_updated_at=body.expected_updated_at,
        )
    except plans_store.PlanConflict as conflict:
        raise HTTPException(
            status_code=409,
            detail={"error": "plan_changed", "current": conflict.current},
        ) from conflict


@router.delete("/{plan_id}")
async def delete_plan(plan_id: str, request: Request) -> dict[str, str]:
    from felix.plans import store as plans_store

    require_mgmt_scopes(request, SCOPE_PLANS_WRITE)
    ok = await plans_store.delete_plan(request.app.state.settings, tenant_id_from_request(request), plan_id)
    if not ok:
        raise HTTPException(status_code=404, detail="not_found")
    return {"status": "deleted"}
