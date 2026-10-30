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
from pydantic import BaseModel, Field, ValidationError

from felix_api.errors import client_safe_message

router = APIRouter(tags=["Jobs"])

# Written only by `schedule_task` and a heartbeat's enrolment; never taken from a caller's payload.
TRUSTED_ORIGIN_KEY = "scheduled_by"


class JobUpsert(BaseModel):
    model_config = {"extra": "forbid"}

    schedule: str = ""
    # The IANA zone a cron `schedule` or `@daily`-style macro is read in, e.g. `America/New_York`;
    # null is UTC. Refused (422) beside an interval, an `at:` or an empty schedule, which no zone
    # moves. Replaced like the rest: a PUT that leaves it out puts the job back on UTC.
    timezone: str | None = Field(default=None, min_length=1, max_length=64)
    manifest_id: str = ""
    # A workflow whose run each firing starts, with `payload.inputs` as its inputs. A job names a
    # manifest or a workflow, never both.
    workflow_id: str = Field(default="", max_length=64)
    # The workflow revision the writer reviewed. When set, the job is written only over that one
    # (else 412 with the current), so an edit saved while they read cannot become what the job runs.
    workflow_revision: str | None = Field(default=None, max_length=128)
    payload: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    # When true, a firing with no reader's preferences (`/jobs/{name}/preferences`) runs nothing and
    # is recorded `skipped`, reason `preferences_missing`. A manifest job's only: refused (422) beside
    # a `workflow_id`, whose firings have no prompt to carry them.
    preferences_required: bool = False


class PreferencesPut(BaseModel):
    model_config = {"extra": "forbid"}

    # Free text, read whole by every firing. The byte cap is `PREFERENCES_MAX_BYTES` (422 past it);
    # an empty document is a DELETE.
    preferences: str = Field(min_length=1)


class PreferencesOut(BaseModel):
    name: str
    # The reader's document, or null when there is none.
    preferences: str | None = None
    # When and by whom it was last written or cleared; null before either.
    updated_at: int | None = None
    updated_by: str | None = None
    # The job's `preferences_required`: whether a firing without them is skipped.
    required: bool = False


def _public(job: dict[str, Any]) -> dict[str, Any]:
    """A job as a reader sees it: who wrote it and when that was recorded (`recorded_at`, null for
    a job written before writers were), but not the scopes and pin its runs carry -- nor its
    preferences' text, which `GET /jobs/{name}/preferences` serves; only whether it has some."""
    from felix.jobs.preferences import has_preferences

    recorded = job.get("created_auth")
    at = recorded.get("recorded_at") if isinstance(recorded, dict) else None
    hidden = {"created_auth", "preferences"}
    return {
        **{k: v for k, v in job.items() if k not in hidden},
        "recorded_at": at,
        "preferences_set": has_preferences(job),
    }


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


async def _workflow_revision(request: Request, tenant_id: str, body: JobUpsert) -> str:
    """The revision of the workflow a job names, which its firings are held to; refuses a writer
    without `workflows:write`, a workflow that does not exist, and inputs it does not take."""
    from felix.auth.mgmt import SCOPE_WORKFLOWS_WRITE
    from felix.workflows import engine
    from felix.workflows import store as workflow_store
    from felix.workflows.schema import WorkflowSpec

    require_mgmt_scopes(request, SCOPE_WORKFLOWS_WRITE)
    found = await workflow_store.get_workflow(request.app.state.settings, tenant_id, body.workflow_id)
    if found is None:
        raise HTTPException(status_code=422, detail={"error": "unknown_workflow"})
    if body.workflow_revision is not None and body.workflow_revision != found["revision"]:
        raise HTTPException(
            status_code=412, detail={"error": "workflow_changed", "revision": found["revision"]}
        )
    inputs = body.payload.get("inputs", {})
    if not isinstance(inputs, dict):
        raise HTTPException(status_code=422, detail={"error": "bad_inputs", "missing": [], "unknown": []})
    try:
        engine.check_inputs(WorkflowSpec.model_validate(found["definition"]), inputs)
    except ValidationError:
        # A stored definition that no longer parses: no run of it could start.
        raise HTTPException(status_code=422, detail={"error": "not_runnable"}) from None
    except engine.BadInputs as bad:
        raise HTTPException(
            status_code=422, detail={"error": "bad_inputs", "missing": bad.missing, "unknown": bad.unknown}
        ) from None
    return str(found["revision"])


async def _payload_to_store(
    settings: Any, tenant_id: str, name: str, sent: dict[str, Any], writer: str
) -> dict[str, Any]:
    """`sent` without any `scheduled_by` it carries, and with the stored job's when `writer` is
    the one who wrote that job."""
    from felix.jobs import store as jobs_store

    payload = {k: v for k, v in sent.items() if k != TRUSTED_ORIGIN_KEY}
    existing = await jobs_store.get_job(settings, tenant_id, name)
    if existing is not None and writer and existing.get("created_by") == writer:
        origin = jobs_store.scheduled_by(existing)
        if origin is not None:
            payload[TRUSTED_ORIGIN_KEY] = origin
    return payload


@router.put("/{name}")
async def upsert_job(name: str, body: JobUpsert, request: Request) -> Any:
    """Create or replace a job. It runs as `cron` on behalf of whoever wrote it last, with their
    scopes: the manifest's inbound auth is checked against them on every firing, so writing a job
    reaches no manifest the writer could not run by asking.

    A job naming a workflow starts a run of it on each firing, and writing one needs
    `workflows:write` beside `jobs:write`, as starting a run does. The job keeps the workflow's
    revision -- `workflow_revision` if the writer names the one they reviewed (412 if it is no
    longer current), else the current one -- and runs only that: after anyone saves the workflow
    again, the job refuses to fire until it is re-saved, so a later edit never runs on the writer's
    scopes unless the writer saves the job over it.

    The job's reader's preferences (`/jobs/{name}/preferences`) are not part of the body: a
    re-save by the job's writer carries them over, and one by anyone else clears them -- they were
    written for the earlier writer's runs, and must not run on the new writer's scopes.

    `payload.scheduled_by` in the body is ignored, not refused. It is the record `schedule_task`
    alone writes -- what marks a job as an agent's task, and the conversation it was scheduled
    from, which a firing's `notify` links to and writes into -- so a caller never supplies one.
    The stored one is carried forward when the task's own writer re-saves it (pausing and resuming
    it re-sends the whole job), so it stays a task. Anyone else re-saving it makes it an ordinary
    job, since carrying the record would let them write into its writer's conversation.
    """
    from felix.context import try_get_context
    from felix.jobs import store as jobs_store
    from felix.jobs.schedule import ScheduleError
    from felix.jobs.scheduler import creator_auth

    require_mgmt_scopes(request, SCOPE_JOBS_WRITE)
    settings = request.app.state.settings
    tenant_id = tenant_id_from_request(request)
    if body.manifest_id and body.workflow_id:
        raise HTTPException(status_code=422, detail={"error": "manifest_and_workflow"})
    if body.preferences_required and body.workflow_id:
        raise HTTPException(status_code=422, detail={"error": "preferences_need_a_manifest"})
    revision = await _workflow_revision(request, tenant_id, body) if body.workflow_id else ""
    ctx = try_get_context()
    if ctx is None:
        # AuthMiddleware installs the context on every request; without it there is no writer
        # to record, and a job recorded as no one would run with no scopes -- refuse instead.
        raise HTTPException(status_code=500, detail="auth_misconfigured")
    # The person, when a machine is writing for them: the same reading every recorder makes.
    writer = ctx.auth.acting_for
    payload = await _payload_to_store(settings, tenant_id, name, body.payload, writer)
    try:
        row = await jobs_store.put_job(
            settings,
            tenant_id,
            name,
            schedule=body.schedule,
            timezone=body.timezone,
            manifest_id=body.manifest_id,
            workflow_id=body.workflow_id,
            workflow_revision=revision,
            payload=payload,
            enabled=body.enabled,
            created_by=writer,
            created_auth=creator_auth(ctx.auth),
            preferences_required=body.preferences_required,
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
    from felix.jobs import store as jobs_store
    from felix.jobs.scheduler import fire_job, now_ms

    require_mgmt_scopes(request, SCOPE_JOBS_WRITE)
    settings = request.app.state.settings
    tenant = tenant_id_from_request(request)
    job = await jobs_store.get_job(settings, tenant, name)
    if job is None:
        raise HTTPException(status_code=404, detail="not_found")
    # `shared_ok`: this route has always matched the writer by subject alone, a shared one
    # included; the caller also holds `jobs:write` here, so a shared key's holders could re-save the
    # job and run it as their own anyway.
    if not _is_job_writer(job, shared_ok=True) and not is_operator(
        settings, auth_from_request(request).principal.scopes
    ):
        raise HTTPException(status_code=403, detail="not_job_writer")
    auth = getattr(request.state, "auth", None)
    who = str(getattr(getattr(auth, "principal", None), "subject", "") or "")
    started_at = now_ms()
    # Not beside a run already in flight, scheduled or asked for: both would share the job's
    # thread. Refused rather than queued -- the caller is waiting on this response, and the run in
    # flight may take as long as its manifest allows. A mark left by a crashed worker stops
    # refusing after `RUNNING_STALE_MS`.
    if not await jobs_store.claim_manual_run(settings, tenant, name, last_run_at=started_at):
        if await jobs_store.get_job(settings, tenant, name) is None:
            raise HTTPException(status_code=404, detail="not_found")
        raise HTTPException(status_code=409, detail={"error": "job_running"})
    return await fire_job(settings, tenant, job, started_at=started_at, trigger="manual", requested_by=who)


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


def _is_job_writer(job: dict[str, Any] | None, *, shared_ok: bool = False) -> bool:
    """Whether the caller is who `job` runs for: its `created_by`, which a machine acting for that
    person also matches (`acting_for`). A named person only (`require_person`) unless `shared_ok`:
    a subject every holder of a credential shares -- a sub-less API key's `api_key` -- names no
    one, and matching on it would make one holder's job every holder's."""
    from felix.context import try_get_context
    from felix.tools.refusal import ToolRefused, require_person

    ctx = try_get_context()
    asker = ctx.auth.acting_for if ctx is not None else ""
    writer = str((job or {}).get("created_by") or "")
    if not writer or asker != writer:
        return False
    if shared_ok:
        return True
    try:
        require_person(no_caller="", shared_subject="")
    except ToolRefused:
        return False
    return True


async def _writer_or_operator_job(request: Request, name: str) -> tuple[dict[str, Any], str]:
    """The job `name`, for its writer or an operator only, and who the caller acts for.

    The writer -- the person it runs for, whose scopes and memory every firing carries -- or an
    operator (`admin` / `*`), as for `POST /jobs/{name}/run`. `jobs:write` is not enough: what this
    guards (the reader's preferences, the runs' state) belongs to the writer's runs and may be
    personal. The writer needs no jobs scope at all, so a person steers the task an agent scheduled
    for them, or their heartbeat. Anyone else gets the same 403 whether or not the job exists; an
    operator gets 404 for one that does not."""
    from felix.auth.mgmt import SCOPE_ADMIN, auth_from_request, is_operator, missing_scopes
    from felix.context import try_get_context
    from felix.jobs import store as jobs_store

    settings = request.app.state.settings
    job = await jobs_store.get_job(settings, tenant_id_from_request(request), name)
    if not _is_job_writer(job) and not is_operator(settings, auth_from_request(request).principal.scopes):
        # Not the writer, so the one way in left is an operator's scope.
        raise missing_scopes(SCOPE_ADMIN)
    if job is None:
        raise HTTPException(status_code=404, detail="not_found")
    ctx = try_get_context()
    return job, ctx.auth.acting_for if ctx is not None else ""


async def _preferences_job(request: Request, name: str) -> tuple[dict[str, Any], str]:
    """The job whose preferences the caller asks for, and who the caller acts for: its writer or an
    operator (`_writer_or_operator_job`). A holder of `jobs:write` who could write another person's
    preferences would put their own words into that person's runs without re-saving the job, which
    is what moves a job to whoever writes it. A workflow job is refused (422): its firings have no
    prompt to carry preferences, so a document there would do nothing."""
    job, who = await _writer_or_operator_job(request, name)
    if job.get("workflow_id"):
        raise HTTPException(status_code=422, detail={"error": "preferences_need_a_manifest"})
    return job, who


def _preferences_out(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": job["name"],
        "preferences": job.get("preferences"),
        "updated_at": job.get("preferences_updated_at"),
        "updated_by": job.get("preferences_updated_by"),
        "required": bool(job.get("preferences_required")),
    }


@router.get("/{name}/preferences", response_model=PreferencesOut)
async def get_job_preferences(name: str, request: Request) -> dict[str, Any]:
    """The job's reader's preferences: what every firing of it reads, in full. The job's writer or an
    operator only -- the text may be personal; `GET /jobs` says only whether there is any."""
    job, _who = await _preferences_job(request, name)
    return _preferences_out(job)


async def _write_preferences(request: Request, name: str, text: str | None) -> dict[str, Any]:
    """Write (or, with None, clear) the preferences and audit it -- never the text, which is the
    reader's and may be personal; its size says enough."""
    from felix.audit import store as audit_store
    from felix.jobs import store as jobs_store
    from felix.jobs.preferences import PREFERENCES_MAX_BYTES, PreferencesTooLong

    job, who = await _preferences_job(request, name)
    settings = request.app.state.settings
    tenant_id = tenant_id_from_request(request)
    try:
        row = await jobs_store.set_preferences(
            settings, tenant_id, name, text, by=who, writer=job.get("created_by")
        )
    except PreferencesTooLong:
        raise HTTPException(
            status_code=422, detail={"error": "preferences_too_long", "max_bytes": PREFERENCES_MAX_BYTES}
        ) from None
    if row is None:
        if await jobs_store.get_job(settings, tenant_id, name) is not None:
            # Re-saved by someone else since the check: their job now, and not this caller's to steer.
            raise HTTPException(status_code=409, detail={"error": "job_changed_hands"})
        raise HTTPException(status_code=404, detail="not_found")
    audit_store.record_event(
        settings,
        tenant_id,
        "job.preferences",
        manifest_id=str(job.get("manifest_id") or ""),
        principal_subj=who,
        status="cleared" if text is None else "saved",
        payload={"job": name, "bytes": len(text.encode("utf-8")) if text is not None else 0},
    )
    return _preferences_out(row)


@router.put("/{name}/preferences", response_model=PreferencesOut)
async def put_job_preferences(name: str, body: PreferencesPut, request: Request) -> dict[str, Any]:
    """Write the preferences the job's reader holds for its output: what to cover, skip, its tone
    and length. Every firing carries them whole in its prompt, fenced as the reader's, and they can
    grant no tool nor change where the output goes. The job's writer or an operator only -- never
    the agent. Re-saving the job (`PUT /jobs/{name}`) keeps them for the same writer and clears them
    for a new one, whose scopes they would otherwise run on."""
    return await _write_preferences(request, name, body.preferences)


@router.delete("/{name}/preferences", response_model=PreferencesOut)
async def delete_job_preferences(name: str, request: Request) -> dict[str, Any]:
    """Clear the job's preferences. A job that requires them then skips its firings until they are
    written again. Who may: as for `PUT`."""
    return await _write_preferences(request, name, None)


class StateEntryOut(BaseModel):
    key: str
    # The text the job's runs stored. Written by the agent, and may hold text it copied from a source.
    value: str
    bytes: int
    updated_at: int
    # The thread of the firing that last wrote it (`/sessions/{thread_id}`), or "".
    updated_by_thread: str = ""


class StateOut(BaseModel):
    name: str
    # Every key the job holds, by key.
    items: list[StateEntryOut] = Field(default_factory=list)


class StateDeleted(BaseModel):
    name: str
    # How many keys were removed.
    deleted: int


def _audit_state(request: Request, job: dict[str, Any], who: str, status: str, deleted: int) -> None:
    """Audit a person's delete of the job's state: who, and how many keys -- never a key's value."""
    from felix.audit import store as audit_store

    audit_store.record_event(
        request.app.state.settings,
        tenant_id_from_request(request),
        "job.state",
        manifest_id=str(job.get("manifest_id") or ""),
        principal_subj=who,
        status=status,
        payload={"job": job["name"], "keys": deleted},
    )


@router.get("/{name}/state", response_model=StateOut)
async def get_job_state(name: str, request: Request) -> dict[str, Any]:
    """The state the job's runs keep for themselves (`spec.state`): every key, its value, size and
    when it was written. The job's writer or an operator only -- values may be personal, as the
    preferences are. The agent writes these; nobody else does."""
    from felix.jobs import state as job_state

    job, _who = await _writer_or_operator_job(request, name)
    rows = await job_state.list_state(
        request.app.state.settings, tenant_id_from_request(request), job["name"]
    )
    return {"name": job["name"], "items": rows}


@router.delete("/{name}/state", response_model=StateDeleted)
async def delete_job_state(name: str, request: Request) -> dict[str, Any]:
    """Clear every key the job's runs keep, so its next firing starts from nothing -- the way to
    reset a bookmark that went wrong. Who may: as for `GET`. Audited (`job.state`, the key count)."""
    from felix.jobs import state as job_state

    job, who = await _writer_or_operator_job(request, name)
    deleted = await job_state.clear(request.app.state.settings, tenant_id_from_request(request), job["name"])
    _audit_state(request, job, who, "cleared", deleted)
    return {"name": job["name"], "deleted": deleted}


@router.delete("/{name}/state/{key}", response_model=StateDeleted)
async def delete_job_state_key(name: str, key: str, request: Request) -> dict[str, Any]:
    """Remove one key the job's runs keep. 404 when the job holds no such key. Who may: as for `GET`."""
    from felix.jobs import state as job_state

    job, who = await _writer_or_operator_job(request, name)
    if not job_state.valid_key(key):
        raise HTTPException(
            status_code=422, detail={"error": "invalid_key", "pattern": job_state.KEY_PATTERN}
        )
    deleted = await job_state.delete_key(
        request.app.state.settings, tenant_id_from_request(request), job["name"], key
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="not_found")
    _audit_state(request, job, who, "deleted", 1)
    return {"name": job["name"], "deleted": 1}
