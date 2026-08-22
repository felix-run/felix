"""Scheduled jobs CRUD."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from felix.context import try_get_context
from pydantic import BaseModel, Field

router = APIRouter(tags=["Jobs"])


class JobUpsert(BaseModel):
    model_config = {"extra": "forbid"}

    schedule: str = ""
    manifest_id: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


def _tenant(request: Request) -> str:
    ctx = try_get_context()
    if ctx is not None:
        return ctx.auth.tenant_id
    auth = getattr(request.state, "auth", None)
    return getattr(auth, "tenant_id", "default") if auth else "default"


@router.get("")
@router.get("/")
async def list_jobs(request: Request) -> dict[str, Any]:
    from felix.jobs import store as jobs_store

    items = await jobs_store.list_jobs(request.app.state.settings, _tenant(request))
    return {"items": items}


@router.get("/{name}")
async def get_job(name: str, request: Request) -> Any:
    from felix.jobs import store as jobs_store

    row = await jobs_store.get_job(request.app.state.settings, _tenant(request), name)
    if row is None:
        raise HTTPException(status_code=404, detail="not_found")
    return row


@router.put("/{name}")
async def upsert_job(name: str, body: JobUpsert, request: Request) -> Any:
    from felix.jobs import store as jobs_store

    return await jobs_store.put_job(
        request.app.state.settings,
        _tenant(request),
        name,
        schedule=body.schedule,
        manifest_id=body.manifest_id,
        payload=body.payload,
        enabled=body.enabled,
    )


@router.delete("/{name}")
async def delete_job(name: str, request: Request) -> dict[str, str]:
    from felix.jobs import store as jobs_store

    ok = await jobs_store.delete_job(request.app.state.settings, _tenant(request), name)
    if not ok:
        raise HTTPException(status_code=404, detail="not_found")
    return {"status": "deleted"}


@router.get("/{name}/runs")
async def list_job_runs(name: str, request: Request, limit: int = 20) -> dict[str, Any]:
    from felix.jobs import store as jobs_store

    items = await jobs_store.list_runs(request.app.state.settings, _tenant(request), name, limit=limit)
    return {"items": items}
