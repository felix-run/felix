"""Scheduled jobs: CRUD, run history, and running one now."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from felix.auth.mgmt import (
    SCOPE_JOBS_READ,
    SCOPE_JOBS_WRITE,
    require_mgmt_scopes,
    tenant_id_from_request,
)
from pydantic import BaseModel, Field

from felix_api.errors import client_safe_message

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
    from felix.jobs.schedule import ScheduleError

    require_mgmt_scopes(request, SCOPE_JOBS_WRITE)
    try:
        return await jobs_store.put_job(
            request.app.state.settings,
            tenant_id_from_request(request),
            name,
            schedule=body.schedule,
            manifest_id=body.manifest_id,
            payload=body.payload,
            enabled=body.enabled,
        )
    except ScheduleError as exc:
        raise HTTPException(
            status_code=422, detail=client_safe_message(exc, authored_for_clients=True)
        ) from None


@router.delete("/{name}")
async def delete_job(name: str, request: Request) -> dict[str, str]:
    from felix.jobs import store as jobs_store

    require_mgmt_scopes(request, SCOPE_JOBS_WRITE)
    ok = await jobs_store.delete_job(request.app.state.settings, tenant_id_from_request(request), name)
    if not ok:
        raise HTTPException(status_code=404, detail="not_found")
    return {"status": "deleted"}


@router.post("/{name}/run")
async def run_job_now(name: str, request: Request) -> Any:
    """Run a job now, without waiting for its schedule, and return the run.

    Synchronous: the response is the finished run, so a new job can be tried before it is
    left to cron. It runs exactly as a scheduled firing does — as `cron`, on the job's thread,
    its prompt screened — and only the run record says otherwise (`trigger: manual`, and who
    asked). The schedule is left alone, and a disabled job runs: asking is explicit.
    `jobs:write`, because whoever may rewrite the prompt may already make it run.
    """
    from felix.jobs import store as jobs_store
    from felix.jobs.scheduler import fire_job, now_ms

    require_mgmt_scopes(request, SCOPE_JOBS_WRITE)
    settings = request.app.state.settings
    tenant = tenant_id_from_request(request)
    job = await jobs_store.get_job(settings, tenant, name)
    if job is None:
        raise HTTPException(status_code=404, detail="not_found")
    auth = getattr(request.state, "auth", None)
    who = str(getattr(getattr(auth, "principal", None), "subject", "") or "")
    return await fire_job(settings, tenant, job, started_at=now_ms(), trigger="manual", requested_by=who)


@router.get("/{name}/runs")
async def list_job_runs(
    name: str, request: Request, limit: int = Query(default=20, ge=1, le=200)
) -> dict[str, Any]:
    from felix.jobs import store as jobs_store

    require_mgmt_scopes(request, SCOPE_JOBS_READ)
    items = await jobs_store.list_runs(
        request.app.state.settings, tenant_id_from_request(request), name, limit=limit
    )
    return {"items": items}
