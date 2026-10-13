"""Runs of authored workflows: start one, watch it, cancel it.

Mounted beside `workflows.py` under `/workflows`. A run starts on the workflow's current revision
and keeps that definition for its life (`felix/workflows/runs.py`), so editing the workflow
mid-run changes nothing it does. `POST /workflows/{id}/runs` answers `202` with the run; the
orchestrator fiber in the worker does the rest (`felix/workflows/engine.py`), and a client polls
`GET /workflows/{id}/runs/{run_id}` for every step's state — an agent step names the thread its
transcript is on, an approval step the approval it is waiting on.

Starting and cancelling need `workflows:write`: a run executes the manifests its steps name, as
the person who started it and with their scopes, recorded the way a durable chat records them.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Path, Query, Request
from felix.auth.mgmt import (
    SCOPE_WORKFLOWS_READ,
    SCOPE_WORKFLOWS_WRITE,
    require_mgmt_scopes,
    subject_from_request,
    tenant_id_from_request,
)
from felix.workflows import engine
from felix.workflows import runs as run_store
from felix.workflows import store as workflow_store
from felix.workflows.check import ProblemCode
from felix.workflows.runs import RunStatus, StepStatus
from felix.workflows.schema import WORKFLOW_ID_PATTERN
from pydantic import BaseModel, Field

router = APIRouter(tags=["Workflows"])

_WORKFLOW_ID = Path(pattern=WORKFLOW_ID_PATTERN, max_length=64)
_RUN_ID = Path(pattern=r"^[0-9a-f]{32}$")


class RunStart(BaseModel):
    model_config = {"extra": "forbid"}

    # One value per workflow input, by name: every declared input, and nothing else.
    inputs: dict[str, Any] = Field(default_factory=dict)
    # The revision the caller read. When set, the run starts only on that revision (else 412),
    # so a co-author's save between reading the workflow and starting it cannot swap what runs.
    revision: str | None = Field(default=None, max_length=128)


class StepRunOut(BaseModel):
    status: StepStatus
    inputs: dict[str, Any] | None = None
    outputs: dict[str, Any] | None = None
    # The control ports this step fired: a branch's case, an approval's `approved`/`denied`.
    fired: list[str] | None = None
    error: str | None = None
    # An agent step's child run and the thread its transcript is on.
    thread_id: str | None = None
    fiber_id: str | None = None
    # An approval step's request, and who decided it.
    approval_id: str | None = None
    decided_by: str | None = None
    started_at: int | None = None
    finished_at: int | None = None


class RunOut(BaseModel):
    id: str
    workflow_id: str
    revision: str
    status: RunStatus
    trigger: str
    started_by: str
    fiber_id: str
    error: str
    expires_at: int
    version: int
    created_at: int
    updated_at: int
    finished_at: int | None
    inputs: dict[str, Any]
    steps: dict[str, StepRunOut]


class RunSummary(BaseModel):
    id: str
    revision: str
    status: RunStatus
    trigger: str
    started_by: str
    error: str
    created_at: int
    finished_at: int | None
    # Each step's status, in definition order: the run strip's row of dots.
    step_statuses: dict[str, StepStatus]


class RunList(BaseModel):
    items: list[RunSummary]


class NotRunnableProblem(BaseModel):
    code: ProblemCode
    message: str
    step: str | None = None
    port: str | None = None


class NotRunnableDetail(BaseModel):
    error: Literal["not_runnable"]
    problems: list[NotRunnableProblem]


class BadInputsDetail(BaseModel):
    error: Literal["bad_inputs"]
    missing: list[str]
    unknown: list[str]


class StartRefused(BaseModel):
    detail: NotRunnableDetail | BadInputsDetail


class NotPermittedDetail(BaseModel):
    error: Literal["not_permitted"]
    # The manifest a step names whose inbound rules refuse the caller.
    manifest: str


class NotPermittedResponse(BaseModel):
    detail: NotPermittedDetail


class TooManyRunsDetail(BaseModel):
    error: Literal["too_many_runs"]
    limit: int


class TooManyRunsResponse(BaseModel):
    detail: TooManyRunsDetail


class RevisionChangedDetail(BaseModel):
    error: Literal["workflow_changed"]
    revision: str


class RevisionChangedResponse(BaseModel):
    detail: RevisionChangedDetail


class NotFound(BaseModel):
    detail: Literal["not_found"]


_NOT_FOUND = {"model": NotFound, "description": "No such workflow or run"}


def _summary(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": run["id"],
        "revision": run["revision"],
        "status": run["status"],
        "trigger": run["trigger"],
        "started_by": run["started_by"],
        "error": run["error"],
        "created_at": run["created_at"],
        "finished_at": run["finished_at"],
        "step_statuses": {step_id: state["status"] for step_id, state in run["steps"].items()},
    }


async def _run_of(request: Request, workflow_id: str, run_id: str) -> dict[str, Any]:
    run = await run_store.get_run(request.app.state.settings, tenant_id_from_request(request), run_id)
    # A run is addressed under its own workflow: another workflow's id in the path is not found.
    if run is None or run["workflow_id"] != workflow_id:
        raise HTTPException(status_code=404, detail="not_found")
    return run


@router.post(
    "/{workflow_id}/runs",
    status_code=202,
    response_model=RunOut,
    responses={
        403: {"model": NotPermittedResponse, "description": "A step names a manifest the caller may not run"},
        404: _NOT_FOUND,
        409: {"model": TooManyRunsResponse, "description": "The tenant has too many runs going"},
        412: {"model": RevisionChangedResponse, "description": "The workflow is no longer at `revision`"},
        422: {"model": StartRefused, "description": "Not runnable, or inputs do not match"},
    },
)
async def start_run(body: RunStart, request: Request, workflow_id: str = _WORKFLOW_ID) -> dict[str, Any]:
    """Start a run of the workflow's current revision with `inputs`."""
    require_mgmt_scopes(request, SCOPE_WORKFLOWS_WRITE)
    settings = request.app.state.settings
    tenant_id = tenant_id_from_request(request)
    workflow = await workflow_store.get_workflow(settings, tenant_id, workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail="not_found")
    if body.revision is not None and body.revision != workflow["revision"]:
        raise HTTPException(
            status_code=412, detail={"error": "workflow_changed", "revision": workflow["revision"]}
        )
    who = subject_from_request(request)
    try:
        run = await engine.start_run(
            settings, tenant_id, workflow, body.inputs, trigger="manual", started_by=who
        )
    except engine.NotRunnable as refused:
        problems = [
            {"code": p.code, "message": p.message, "step": p.step, "port": p.port} for p in refused.problems
        ]
        raise HTTPException(status_code=422, detail={"error": "not_runnable", "problems": problems}) from None
    except engine.BadInputs as bad:
        raise HTTPException(
            status_code=422, detail={"error": "bad_inputs", "missing": bad.missing, "unknown": bad.unknown}
        ) from None
    except engine.NotPermitted as refused:
        # 401 when the manifest refuses anonymous callers, 403 otherwise: its own rule's answer.
        raise HTTPException(
            status_code=refused.status_code, detail={"error": "not_permitted", "manifest": refused.manifest}
        ) from None
    except engine.TooManyRuns:
        raise HTTPException(
            status_code=409, detail={"error": "too_many_runs", "limit": engine.MAX_ACTIVE_RUNS_PER_TENANT}
        ) from None
    from felix.audit import store as audit_store

    audit_store.record_event(
        settings,
        tenant_id,
        "workflow_run_started",
        principal_subj=who,
        status="ok",
        payload={"workflow_id": workflow_id, "run_id": run["id"], "revision": run["revision"]},
    )
    return run_store.workflow_run_view(run)


@router.get("/{workflow_id}/runs", response_model=RunList)
async def list_runs(
    request: Request,
    workflow_id: str = _WORKFLOW_ID,
    limit: int = Query(default=50, ge=1, le=run_store.MAX_RUN_LIST),
) -> dict[str, Any]:
    """The workflow's runs, newest first, each with its steps' statuses."""
    require_mgmt_scopes(request, SCOPE_WORKFLOWS_READ)
    runs = await run_store.list_runs(
        request.app.state.settings, tenant_id_from_request(request), workflow_id, limit=limit
    )
    return {"items": [_summary(r) for r in runs]}


@router.get("/{workflow_id}/runs/{run_id}", response_model=RunOut, responses={404: _NOT_FOUND})
async def get_run(request: Request, workflow_id: str = _WORKFLOW_ID, run_id: str = _RUN_ID) -> dict[str, Any]:
    require_mgmt_scopes(request, SCOPE_WORKFLOWS_READ)
    return run_store.workflow_run_view(await _run_of(request, workflow_id, run_id))


@router.post("/{workflow_id}/runs/{run_id}/cancel", response_model=RunOut, responses={404: _NOT_FOUND})
async def cancel_run(
    request: Request, workflow_id: str = _WORKFLOW_ID, run_id: str = _RUN_ID
) -> dict[str, Any]:
    """Ask the run to stop: `cancelling` until the orchestrator aborts what is in flight.

    A run already over is returned as it is.
    """
    require_mgmt_scopes(request, SCOPE_WORKFLOWS_WRITE)
    await _run_of(request, workflow_id, run_id)
    settings = request.app.state.settings
    tenant_id = tenant_id_from_request(request)
    run = await engine.request_cancel(settings, tenant_id, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="not_found")
    from felix.audit import store as audit_store

    audit_store.record_event(
        settings,
        tenant_id,
        "workflow_run_cancelled",
        principal_subj=subject_from_request(request),
        status="ok",
        payload={"workflow_id": workflow_id, "run_id": run_id},
    )
    return run_store.workflow_run_view(run)
