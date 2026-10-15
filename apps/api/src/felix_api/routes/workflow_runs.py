"""Runs of authored workflows: start one, watch it, cancel it.

Mounted beside `workflows.py` under `/workflows`. A run starts on the workflow's current revision
and keeps that definition for its life (`felix/workflows/runs.py`), so editing the workflow
mid-run changes nothing it does. `POST /workflows/{id}/runs` answers `202` with the run; the
orchestrator fiber in the worker does the rest (`felix/workflows/engine.py`), and a client polls
`GET /workflows/{id}/runs/{run_id}` for every step's state — an agent step names the thread its
transcript is on, an approval step the approval it is waiting on — or watches
`GET /workflows/{id}/runs/{run_id}/stream` for the same record as it changes, and reads a step's
transcript at `GET /workflows/{id}/runs/{run_id}/steps/{step_id}/thread`.

Starting and cancelling need `workflows:write`: a run executes the manifests its steps name, as
the person who started it and with their scopes, recorded the way a durable chat records them.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Path, Query, Request
from fastapi.responses import StreamingResponse
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
from felix.workflows.schema import STEP_ID_PATTERN, WORKFLOW_ID_PATTERN
from pydantic import BaseModel, Field

from felix_api.routes._sse import DONE, KEEP_ALIVE, error_frame, frame, sse_response

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Workflows"])

# How long the stream waits for a run to move before reading it anyway: short when nothing can wake
# it (no Redis, so a save in the worker is announced only in the worker's own process), long when
# saves are announced and the read is a safety net.
STREAM_POLL_SECONDS = 2.0
STREAM_NOTIFIED_POLL_SECONDS = 30.0

_WORKFLOW_ID = Path(pattern=WORKFLOW_ID_PATTERN, max_length=64)
_RUN_ID = Path(pattern=r"^[0-9a-f]{32}$")
_STEP_ID = Path(pattern=STEP_ID_PATTERN)


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
    # The inputs a third party supplied (a trigger's payload). While any are, every value an agent
    # step's prompt is filled with is fenced as data, not instructions.
    untrusted_inputs: list[str]
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
_NO_THREAD = {
    "model": NotFound,
    "description": "No such workflow, run or step, or the step has no thread yet",
}


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
    who = subject_from_request(request)
    try:
        run = await engine.start_run(
            settings,
            tenant_id,
            workflow,
            body.inputs,
            trigger="manual",
            started_by=who,
            revision=body.revision,
        )
    except engine.RevisionChanged as changed:
        raise HTTPException(
            status_code=412, detail={"error": "workflow_changed", "revision": changed.current}
        ) from None
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


@router.get("/{workflow_id}/runs/{run_id}/steps/{step_id}/thread", responses={404: _NO_THREAD})
async def get_step_thread(
    request: Request, workflow_id: str = _WORKFLOW_ID, run_id: str = _RUN_ID, step_id: str = _STEP_ID
) -> dict[str, Any]:
    """The transcript of one step's child run: the snapshot `GET /chat/sessions/{id}` answers.

    Read here, under `workflows:read`, because no chat route can address the thread: it is in the
    harness's own namespace (`thread_ids.workflow_thread_id`), which is what keeps a chat client
    from writing into a step's history — a steer, a custom entry — and having the step's agent read
    it as its own, under the starter's authority. This route only reads.
    """
    from felix.session.snapshot import gather_thread_snapshot
    from felix.thread_ids import thread_belongs_to_tenant

    require_mgmt_scopes(request, SCOPE_WORKFLOWS_READ)
    run = await _run_of(request, workflow_id, run_id)
    tenant_id = tenant_id_from_request(request)
    thread = str((run["steps"].get(step_id) or {}).get("thread_id") or "")
    if not thread_belongs_to_tenant(tenant_id, thread):
        raise HTTPException(status_code=404, detail="not_found")
    return await gather_thread_snapshot(
        settings=request.app.state.settings, tenant_id=tenant_id, thread=thread
    )


def _run_out(run: dict[str, Any]) -> dict[str, Any]:
    """A run as `GET /workflows/{id}/runs/{run_id}` answers it, through the same model."""
    return RunOut.model_validate(run_store.workflow_run_view(run)).model_dump(mode="json")


async def _run_frames(settings: Any, tenant_id: str, run: dict[str, Any], seen: int | None) -> Any:
    """`workflow_run` frames, one per version of the run's record, until the run is over.

    Each frame is the record `GET /workflows/{id}/runs/{run_id}` answers, its `id:` the record's
    version: a client that reconnects with that as `Last-Event-ID` gets the next change, not the
    one it has. Every save of the run announces itself (`runs._announce`), so a step moving reaches
    the stream as it is written; the read between waits is what keeps a missed announcement from
    costing more than a poll.
    """
    from felix.errors import client_safe_message
    from felix.logging_setup import loggable
    from felix.session.notify import thread_watch
    from felix.thread_ids import workflow_run_key

    key = workflow_run_key(tenant_id, run["id"]) or ""
    try:
        async with thread_watch(tenant_id, key) as watch:
            while True:
                if run["version"] != seen:
                    seen = int(run["version"])
                    yield frame({"event": "workflow_run", "data": _run_out(run)}, cursor=seen)
                if run["status"] in run_store.RUN_TERMINAL:
                    break
                wait = STREAM_NOTIFIED_POLL_SECONDS if watch.delivering else STREAM_POLL_SECONDS
                if not (await watch.wait(timeout=wait)).woken:
                    yield KEEP_ALIVE
                fresh = await run_store.get_run(settings, tenant_id, run["id"])
                if fresh is None:
                    # Swept: retention removed a finished run's record while it was watched.
                    yield error_frame("run_not_found", kind="run_error")
                    break
                run = fresh
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("workflow run stream failed run=%s", loggable(run["id"], limit=64))
        yield error_frame(client_safe_message(exc), kind="run_error")
    yield DONE


@router.get(
    "/{workflow_id}/runs/{run_id}/stream",
    response_class=StreamingResponse,
    responses={
        200: {"content": {"text/event-stream": {}}, "description": "`workflow_run` frames"},
        404: _NOT_FOUND,
    },
)
async def stream_run(
    request: Request, workflow_id: str = _WORKFLOW_ID, run_id: str = _RUN_ID
) -> StreamingResponse:
    """The run as it changes: a `workflow_run` frame for each new version, then `[DONE]` once over.

    The first frame is the run as it stands, unless `Last-Event-ID` (or `last_event_id`) already
    names that version. A finished run is one frame and `[DONE]`. Under `workflows:read`, as the
    record itself is.
    """
    require_mgmt_scopes(request, SCOPE_WORKFLOWS_READ)
    run = await _run_of(request, workflow_id, run_id)
    header = request.headers.get("last-event-id") or request.query_params.get("last_event_id")
    try:
        seen = int(header) if header not in (None, "") else None
    except ValueError:
        seen = None
    return sse_response(_run_frames(request.app.state.settings, tenant_id_from_request(request), run, seen))


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
