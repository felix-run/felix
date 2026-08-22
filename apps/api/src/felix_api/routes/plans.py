"""Durable plan store (agent-authored multi-step plans)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from felix.context import try_get_context
from pydantic import BaseModel

router = APIRouter(tags=["Plans"])


class PlanUpsert(BaseModel):
    model_config = {"extra": "forbid"}

    plan: dict[str, Any]
    manifest_id: str = ""
    expires_at: int | None = None


def _tenant(request: Request) -> str:
    ctx = try_get_context()
    if ctx is not None:
        return ctx.auth.tenant_id
    auth = getattr(request.state, "auth", None)
    return getattr(auth, "tenant_id", "default") if auth else "default"


@router.get("")
@router.get("/")
async def list_plans(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    from felix.plans import store as plans_store

    items = await plans_store.list_plans(request.app.state.settings, _tenant(request), limit=limit)
    return {"items": items, "plans": items}


@router.get("/{plan_id}")
async def get_plan(plan_id: str, request: Request) -> Any:
    from felix.plans import store as plans_store

    row = await plans_store.get_plan(request.app.state.settings, _tenant(request), plan_id)
    if row is None:
        raise HTTPException(status_code=404, detail="not_found")
    return row


@router.put("/{plan_id}")
async def upsert_plan(plan_id: str, body: PlanUpsert, request: Request) -> Any:
    from felix.plans import store as plans_store

    return await plans_store.put_plan(
        request.app.state.settings,
        _tenant(request),
        plan_id,
        plan=body.plan,
        manifest_id=body.manifest_id,
        expires_at=body.expires_at,
    )


@router.delete("/{plan_id}")
async def delete_plan(plan_id: str, request: Request) -> dict[str, str]:
    from felix.plans import store as plans_store

    ok = await plans_store.delete_plan(request.app.state.settings, _tenant(request), plan_id)
    if not ok:
        raise HTTPException(status_code=404, detail="not_found")
    return {"status": "deleted"}
