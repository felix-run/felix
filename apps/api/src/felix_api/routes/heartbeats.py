"""Heartbeats: a person turning their own periodic check-in on or off (`felix/jobs/heartbeat.py`).

The operator writes the heartbeat (`spec.heartbeat`: what to check, how often); the caller only
switches theirs on, off, or runs it now. Every route acts on the caller's own heartbeat and no one
else's, so each needs a named person -- not anonymous, not a credential every holder shares -- and
answers 404 for a heartbeat that is not theirs rather than saying whose it is.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request
from felix.auth.mgmt import (
    SCOPE_HEARTBEATS_READ,
    SCOPE_HEARTBEATS_WRITE,
    require_mgmt_scopes,
    tenant_id_from_request,
)
from pydantic import BaseModel, ConfigDict

from felix_api.errors import client_safe_message

if TYPE_CHECKING:
    from felix.context import AuthContext

router = APIRouter(tags=["Heartbeats"])

# Chosen by `ToolRefused.code`, never taken from the exception.
_NOT_A_PERSON = {
    "no_caller": "a heartbeat belongs to a signed-in person, and this request has none",
    "shared_subject": (
        "the credential this request uses names no person (it is shared), so a heartbeat could not "
        "belong to anyone"
    ),
}


class HeartbeatUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # The only thing a person controls: on or off. What it checks and when are the operator's.
    enabled: bool = True


class HeartbeatOut(BaseModel):
    manifest: str
    name: str
    enabled: bool
    schedule: str
    # The IANA zone a cron `schedule` is read in (`spec.heartbeat.timezone`); null is UTC.
    timezone: str | None = None
    # The caller's heartbeat conversation, where each notice is left: a chat thread id.
    thread_id: str | None = None
    next_run_at: int | None = None
    last_run_at: int | None = None
    last_status: str | None = None
    # When it stops for good (ISO 8601): the expiry of the credential that turned it on, for one no
    # firing can re-check (a token from another issuer). Null while each firing re-checks it.
    expires_at: str | None = None


class HeartbeatList(BaseModel):
    items: list[HeartbeatOut]


def _person() -> AuthContext:
    """The caller, who must be a named person; 403 with a written refusal otherwise."""
    from felix.tools.refusal import ToolRefused, require_person

    try:
        _ctx, auth = require_person(
            no_caller=_NOT_A_PERSON["no_caller"], shared_subject=_NOT_A_PERSON["shared_subject"]
        )
    except ToolRefused as refused:
        code = refused.code if refused.code in _NOT_A_PERSON else "no_caller"
        raise HTTPException(status_code=403, detail={"error": code, "detail": _NOT_A_PERSON[code]}) from None
    return auth


async def _mine_or_404(request: Request, manifest: str) -> tuple[AuthContext, str, dict[str, Any]]:
    from felix.jobs import heartbeat as hb

    auth = _person()
    tenant_id = tenant_id_from_request(request)
    job = await hb.get_mine(request.app.state.settings, tenant_id, manifest, auth.acting_for)
    if job is None:
        # As `routes/jobs.py` answers a job that is not there; never whose it is.
        raise HTTPException(status_code=404, detail="not_found")
    return auth, tenant_id, job


@router.get("", response_model=HeartbeatList)
@router.get("/", response_model=HeartbeatList, include_in_schema=False)
async def list_heartbeats(request: Request) -> dict[str, Any]:
    """The caller's own heartbeats, on every manifest."""
    from felix.jobs import heartbeat as hb

    require_mgmt_scopes(request, SCOPE_HEARTBEATS_READ)
    auth = _person()
    found = await hb.mine(request.app.state.settings, tenant_id_from_request(request), auth.acting_for)
    return {"items": [hb.public(job) for job in found]}


@router.put("/{manifest}", response_model=HeartbeatOut)
async def put_heartbeat(manifest: str, body: HeartbeatUpsert, request: Request) -> dict[str, Any]:
    """Turn the caller's heartbeat on `manifest` on (or off, keeping it and its conversation).

    The manifest must define `spec.heartbeat` (422 `no_heartbeat`), and the caller must pass its
    inbound rules as each firing will run them -- `cron` on their behalf, with their scopes -- so a
    heartbeat that could never run is refused here, with the same written message. The first
    firing waits for the schedule. The caller's heartbeat conversation is created on first enrol
    and kept across later ones while it exists.
    """
    from felix.jobs import heartbeat as hb
    from felix.manifests.inbound_auth import InboundAuthError

    require_mgmt_scopes(request, SCOPE_HEARTBEATS_WRITE)
    auth = _person()
    try:
        job = await hb.enrol(
            request.app.state.settings,
            tenant_id=tenant_id_from_request(request),
            auth=auth,
            manifest_id=manifest,
            enabled=body.enabled,
        )
    except hb.HeartbeatRefused as refused:
        raise HTTPException(
            status_code=refused.status, detail={"error": refused.code, "detail": refused.message}
        ) from None
    except InboundAuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=client_safe_message(exc)) from None
    return hb.public(job)


@router.delete("/{manifest}")
async def delete_heartbeat(manifest: str, request: Request) -> dict[str, str]:
    """Turn the caller's heartbeat off for good: its job, run history and firing threads go. Its
    conversation stays -- it holds the notices the person was sent, and is theirs to delete."""
    from felix.jobs import heartbeat as hb
    from felix.jobs import store as jobs_store

    require_mgmt_scopes(request, SCOPE_HEARTBEATS_WRITE)
    _auth, tenant_id, job = await _mine_or_404(request, manifest)
    settings = request.app.state.settings
    await jobs_store.delete_job(settings, tenant_id, job["name"])
    await hb.prune_firing_threads(settings, tenant_id, job, keep=0)
    return {"status": "deleted"}


@router.post("/{manifest}/run")
async def run_heartbeat(manifest: str, request: Request) -> Any:
    """Fire the caller's heartbeat now and return the run, exactly as a scheduled firing runs.
    409 `heartbeat_running` while a firing of it is in flight."""
    from felix.jobs import store as jobs_store
    from felix.jobs.scheduler import fire_job, now_ms

    require_mgmt_scopes(request, SCOPE_HEARTBEATS_WRITE)
    auth, tenant_id, job = await _mine_or_404(request, manifest)
    settings = request.app.state.settings
    started_at = now_ms()
    if not await jobs_store.claim_manual_run(settings, tenant_id, job["name"], last_run_at=started_at):
        raise HTTPException(status_code=409, detail={"error": "heartbeat_running"})
    return await fire_job(
        settings, tenant_id, job, started_at=started_at, trigger="manual", requested_by=auth.acting_for
    )
