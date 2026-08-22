"""Scheduled jobs CRUD."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from felix.auth.mgmt import (
    SCOPE_JOBS_READ,
    SCOPE_JOBS_WRITE,
    require_mgmt_scopes,
    tenant_id_from_request,
)
from pydantic import BaseModel, Field

router = APIRouter(tags=["Jobs"])


class JobUpsert(BaseModel):
    model_config = {"extra": "forbid"}

    schedule: str = ""
    manifest_id: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


@router.get("")
@router.get("/")
async def list_jobs(request: Request) -> dict[str, Any]:
    from felix.jobs import store as jobs_store

    require_mgmt_scopes(request, SCOPE_JOBS_READ)
    items = await jobs_store.list_jobs(request.app.state.settings, tenant_id_from_request(request))
    return {"items": items}


@router.get("/{name}")
async def get_job(name: str, request: Request) -> Any:
    from felix.jobs import store as jobs_store

    require_mgmt_scopes(request, SCOPE_JOBS_READ)
    row = await jobs_store.get_job(request.app.state.settings, tenant_id_from_request(request), name)
    if row is None:
        raise HTTPException(status_code=404, detail="not_found")
    return row


@router.put("/{name}")
async def upsert_job(name: str, body: JobUpsert, request: Request) -> Any:
    from felix.jobs import store as jobs_store

    require_mgmt_scopes(request, SCOPE_JOBS_WRITE)
    return await jobs_store.put_job(
        request.app.state.settings,
        tenant_id_from_request(request),
        name,
        schedule=body.schedule,
        manifest_id=body.manifest_id,
        payload=body.payload,
        enabled=body.enabled,
    )


@router.delete("/{name}")
async def delete_job(name: str, request: Request) -> dict[str, str]:
    from felix.jobs import store as jobs_store

    require_mgmt_scopes(request, SCOPE_JOBS_WRITE)
    ok = await jobs_store.delete_job(request.app.state.settings, tenant_id_from_request(request), name)
    if not ok:
        raise HTTPException(status_code=404, detail="not_found")
    return {"status": "deleted"}


@router.get("/{name}/runs")
async def list_job_runs(name: str, request: Request, limit: int = 20) -> dict[str, Any]:
    from felix.jobs import store as jobs_store

    require_mgmt_scopes(request, SCOPE_JOBS_READ)
    items = await jobs_store.list_runs(
        request.app.state.settings, tenant_id_from_request(request), name, limit=limit
    )
    return {"items": items}
