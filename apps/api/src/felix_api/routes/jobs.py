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


def _public(job: dict[str, Any]) -> dict[str, Any]:
    """A job as a reader sees it: who wrote it and when that was recorded (`recorded_at`, null for
    a job written before writers were), but not the scopes and pin its runs carry."""
    recorded = job.get("created_auth")
    at = recorded.get("recorded_at") if isinstance(recorded, dict) else None
    return {**{k: v for k, v in job.items() if k != "created_auth"}, "recorded_at": at}


@router.get("")
@router.get("/")
async def list_jobs(request: Request) -> dict[str, Any]:
    from felix.jobs import store as jobs_store

    require_mgmt_scopes(request, SCOPE_JOBS_READ)
    items = await jobs_store.list_jobs(request.app.state.settings, tenant_id_from_request(request))
    return {"items": [_public(job) for job in items]}


@router.get("/{name}")
async def get_job(name: str, request: Request) -> Any:
    from felix.jobs import store as jobs_store

    require_mgmt_scopes(request, SCOPE_JOBS_READ)
    row = await jobs_store.get_job(request.app.state.settings, tenant_id_from_request(request), name)
    if row is None:
        raise HTTPException(status_code=404, detail="not_found")
    return _public(row)


@router.put("/{name}")
async def upsert_job(name: str, body: JobUpsert, request: Request) -> Any:
    """Create or replace a job. It runs as `cron` on behalf of whoever wrote it last, with their
    scopes: the manifest's inbound auth is checked against them on every firing, so writing a job
    reaches no manifest the writer could not run by asking."""
    from felix.context import try_get_context
    from felix.jobs import store as jobs_store
    from felix.jobs.schedule import ScheduleError
    from felix.jobs.scheduler import creator_auth

    require_mgmt_scopes(request, SCOPE_JOBS_WRITE)
    ctx = try_get_context()
    if ctx is None:
        # AuthMiddleware installs the context on every request; without it there is no writer
        # to record, and a job recorded as no one would run with no scopes -- refuse instead.
        raise HTTPException(status_code=500, detail="auth_misconfigured")
    # The person, when a machine is writing for them: the same reading every recorder makes.
    writer = ctx.auth.acting_for
    try:
        row = await jobs_store.put_job(
            request.app.state.settings,
            tenant_id_from_request(request),
            name,
            schedule=body.schedule,
            manifest_id=body.manifest_id,
            payload=body.payload,
            enabled=body.enabled,
            created_by=writer,
            created_auth=creator_auth(ctx.auth),
        )
    except ScheduleError as exc:
        raise HTTPException(
            status_code=422, detail=client_safe_message(exc, authored_for_clients=True)
        ) from None
    return _public(row)


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
    left to cron. It runs exactly as a scheduled firing does — as `cron` for the job's writer,
    on the job's thread, its prompt screened — and only the run record says otherwise
    (`trigger: manual`, and who asked). The schedule is left alone, and a disabled job runs:
    asking is explicit.

    `jobs:write`, and either be the job's writer or an operator (`admin` / `*`). The run carries
    the writer's authority, so anyone else asking would be making the writer's run happen at a
    time the writer did not choose -- a disabled job most of all; rewriting the job instead makes
    it theirs, with their own scopes. A job written before writers were recorded is an operator's.
    """
    from felix.auth.mgmt import auth_from_request, is_operator
    from felix.context import try_get_context
    from felix.jobs import store as jobs_store
    from felix.jobs.scheduler import fire_job, now_ms

    require_mgmt_scopes(request, SCOPE_JOBS_WRITE)
    settings = request.app.state.settings
    tenant = tenant_id_from_request(request)
    job = await jobs_store.get_job(settings, tenant, name)
    if job is None:
        raise HTTPException(status_code=404, detail="not_found")
    ctx = try_get_context()
    asker = ctx.auth.acting_for if ctx is not None else ""
    writer = str(job.get("created_by") or "")
    if not (writer and asker == writer) and not is_operator(
        settings, auth_from_request(request).principal.scopes
    ):
        raise HTTPException(status_code=403, detail="not_job_writer")
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
