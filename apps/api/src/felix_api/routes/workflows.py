"""Authored workflows: definitions, their ports and problems, and conditional saves.

A workflow is saved whole (`PUT /workflows/{id}`, the body is the definition) and every response
that carries one also carries what the editor draws from it: each node's resolved ports and the
problems that stop it running. A definition with problems saves — the editor saves half-wired
drafts — and only a run refuses one.

Saves are conditional through HTTP's own headers. Every response carries the row's revision as
its `ETag`; `If-Match: "<revision>"` saves only over that revision and `If-None-Match: *` only
where no workflow exists, and a miss is `412` with the workflow as it now stands, so the editor
can offer *keep mine* or *take theirs* instead of one save silently erasing the other; the 412
carries the current revision as its `ETag` too. A save with neither header is unconditional, and
loses only a race to create the same id, which is `409` with the same body (412 is for a
precondition the caller sent). `PUT` with `If-Match` on a workflow that is gone is 412 with
`current: null`; `DELETE` of one is 404. An editor treats both as *deleted elsewhere*.

`/-/kinds` lists the step kinds the registry holds, `/-/check` checks a definition without
saving it, and `/-/tools?manifest=` lists the tools a `tool_call` step naming that manifest can
call, compiled as the caller. The `-/` prefix cannot collide with a workflow, whose id starts
with a letter or digit.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Header, HTTPException, Path, Query, Request, Response
from felix.auth.mgmt import (
    SCOPE_WORKFLOWS_READ,
    SCOPE_WORKFLOWS_WRITE,
    require_mgmt_scopes,
    subject_from_request,
    tenant_id_from_request,
)
from felix.context import AuthContext, RequestContext, async_run_with_context, try_get_context
from felix.logging_setup import loggable
from felix.tools.types import tool_input_schema
from felix.workflows import store as workflow_store
from felix.workflows.check import Problem, ProblemCode, check
from felix.workflows.kinds import get_step_kind, list_step_kinds
from felix.workflows.ports import Port, StepPorts, describe
from felix.workflows.schema import WORKFLOW_ID_PATTERN, WorkflowSpec
from felix.workflows.tool_step import step_ports, step_tools
from pydantic import BaseModel, ConfigDict, Field, ValidationError

logger = logging.getLogger("felix_api.routes.workflows")

router = APIRouter(tags=["Workflows"])

_WORKFLOW_ID = Path(pattern=WORKFLOW_ID_PATTERN, max_length=64)


class PortOut(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str
    # The type as the editor labels it: `string`, `array<object>`, `enum<bug|feature>`.
    type: str
    schema_: dict[str, Any] = Field(alias="schema")
    control: bool
    required: bool


class StepPortsOut(BaseModel):
    inputs: list[PortOut]
    outputs: list[PortOut]


class ProblemOut(BaseModel):
    code: ProblemCode
    message: str
    step: str | None = None
    port: str | None = None


class CheckOut(BaseModel):
    # Every node, `trigger` included; `null` where a step's ports cannot be known (its kind is
    # missing or its config invalid — the problems say which).
    ports: dict[str, StepPortsOut | None]
    problems: list[ProblemOut]


class WorkflowOut(CheckOut):
    id: str
    version: int
    revision: str
    definition: dict[str, Any]
    created_at: int
    updated_at: int
    updated_by: str
    # Who created it, and what made its latest save: `agent:<manifest>` while the agent that wrote
    # it for `created_by` still holds it, empty once a person saves it through these routes.
    created_by: str
    authored_via: str


class WorkflowSummary(BaseModel):
    id: str
    name: str
    description: str
    version: int
    revision: str
    step_count: int
    updated_at: int
    updated_by: str
    created_by: str
    authored_via: str


class WorkflowList(BaseModel):
    items: list[WorkflowSummary]
    # How many workflows the tenant may keep, and so how many one listing shows (two creates racing
    # at the cap can both land, leaving one past it off the page until something is deleted).
    limit: int


class StepKindOut(BaseModel):
    name: str
    label: str
    # The JSON Schema of the kind's `config`, for the editor's step form.
    config_schema: dict[str, Any]


class StepKindList(BaseModel):
    items: list[StepKindOut]


class StepToolOut(BaseModel):
    name: str
    description: str
    # What a `tool_call` step stores as its `input_schema` when it is wired to this tool.
    input_schema: dict[str, Any]
    # The step's input ports with no `args` fixed; `null` when the schema cannot become ports.
    inputs: list[PortOut] | None
    # Why `inputs` is `null`: the tool cannot be wired as a step.
    problem: str | None = None
    read_only: bool
    # Whether a run whose worker died mid-call may call it again (`replay_safe`).
    replay_safe: bool


class StepToolList(BaseModel):
    manifest: str
    items: list[StepToolOut]


class ManifestRefusedDetail(BaseModel):
    error: Literal["unknown_manifest", "not_permitted", "manifest_unavailable"]
    manifest: str


class ManifestRefused(BaseModel):
    detail: ManifestRefusedDetail


class WorkflowChanged(BaseModel):
    error: Literal["workflow_changed"]
    # The workflow as it stands now, presented like any other response; `null` when it is gone.
    current: WorkflowOut | None


class WorkflowChangedResponse(BaseModel):
    detail: WorkflowChanged


class WorkflowLimit(BaseModel):
    error: Literal["workflow_limit"]
    limit: int


class WorkflowLimitResponse(BaseModel):
    detail: WorkflowLimit | WorkflowChanged


class NotFoundResponse(BaseModel):
    detail: Literal["not_found"]


_CHANGED = {"model": WorkflowChangedResponse, "description": "The precondition named another revision"}
_NOT_FOUND = {"model": NotFoundResponse, "description": "No such workflow"}


def _port(port: Port) -> dict[str, Any]:
    return {
        "name": port.name,
        "type": "control" if port.control else describe(port.schema),
        "schema": port.schema,
        "control": port.control,
        "required": port.required,
    }


def _ports(resolved: StepPorts | None) -> dict[str, Any] | None:
    if resolved is None:
        return None
    return {"inputs": [_port(p) for p in resolved.inputs], "outputs": [_port(p) for p in resolved.outputs]}


def _problem(problem: Problem) -> dict[str, Any]:
    return {"code": problem.code, "message": problem.message, "step": problem.step, "port": problem.port}


def _checked(spec: WorkflowSpec) -> dict[str, Any]:
    ports, found = check(spec)
    return {"ports": {node: _ports(p) for node, p in ports.items()}, "problems": [_problem(p) for p in found]}


def _present(row: dict[str, Any]) -> dict[str, Any]:
    """A stored row as the API returns it: the row, its ports and its problems.

    A definition a later schema no longer parses is reported as `invalid_definition` problems
    rather than failing the read: the store hands definitions back unparsed for exactly this.
    """
    out = {
        k: row[k]
        for k in (
            "id",
            "version",
            "revision",
            "definition",
            "created_at",
            "updated_at",
            "updated_by",
            "created_by",
            "authored_via",
        )
    }
    try:
        spec = WorkflowSpec.model_validate(row["definition"])
    except ValidationError as exc:
        return out | {
            "ports": {},
            "problems": [
                {
                    "code": "invalid_definition",
                    # Pydantic's own messages name the location and the rule, not the input.
                    "message": f"{'.'.join(str(p) for p in err['loc']) or 'definition'}: {err['msg']}",
                    "step": None,
                    "port": None,
                }
                for err in exc.errors()
            ],
        }
    return out | _checked(spec)


def _summary(row: dict[str, Any]) -> dict[str, Any]:
    definition = row["definition"]
    steps = definition.get("steps")
    return {
        "id": row["id"],
        "name": str(definition.get("name", "")),
        "description": str(definition.get("description", "")),
        "version": row["version"],
        "revision": row["revision"],
        "step_count": len(steps) if isinstance(steps, list) else 0,
        "updated_at": row["updated_at"],
        "updated_by": row["updated_by"],
        "created_by": row["created_by"],
        "authored_via": row["authored_via"],
    }


def _etag(row: dict[str, Any]) -> str:
    # A revision is a uuid4 hex and an integer joined by a dot, so it needs no escaping here.
    return f'"{row["revision"]}"'


def _precondition(if_match: str | None, if_none_match: str | None) -> workflow_store.Precondition:
    """The store precondition the request's conditional headers ask for."""
    if if_match is not None and if_none_match is not None:
        raise HTTPException(status_code=400, detail="conflicting_preconditions")
    if if_none_match is not None:
        if if_none_match.strip() != "*":
            # Only `*` means anything here: a save is not a cached read.
            raise HTTPException(status_code=400, detail="if_none_match_must_be_star")
        return workflow_store.ABSENT
    if if_match is None:
        return None
    tag = if_match.strip()
    if tag == "*" or "," in tag:
        # "Any revision" and a list of revisions are both legal HTTP and neither is a thing an
        # editor sends; refusing them beats guessing what the caller meant.
        raise HTTPException(status_code=400, detail="if_match_must_name_one_revision")
    # A weak tag keeps its `W/` and so matches nothing: If-Match compares strongly.
    if len(tag) >= 2 and tag[0] == tag[-1] == '"':
        tag = tag[1:-1]
    return tag


def _changed(conflict: workflow_store.WorkflowConflict, *, conditional: bool = True) -> HTTPException:
    current = _present(conflict.current) if conflict.current is not None else None
    headers = {"ETag": _etag(conflict.current)} if conflict.current is not None else None
    return HTTPException(
        status_code=412 if conditional else 409,
        detail={"error": "workflow_changed", "current": current},
        headers=headers,
    )


@router.get("/-/kinds", response_model=StepKindList)
async def list_kinds(request: Request) -> dict[str, Any]:
    """The step kinds a workflow can use: built in, and any a plugin registered."""
    require_mgmt_scopes(request, SCOPE_WORKFLOWS_READ)
    items = []
    for name in list_step_kinds():
        kind = get_step_kind(name)
        if kind is None:
            continue
        try:
            config_schema = kind.config.model_json_schema()
        except Exception:
            # A plugin's config model pydantic cannot describe is that kind's problem: listed with
            # no form, never a 500 that empties the editor's palette.
            logger.warning("step kind %s has no JSON Schema for its config", name, exc_info=True)
            config_schema = {}
        items.append({"name": name, "label": kind.label, "config_schema": config_schema})
    return {"items": items}


def _step_tool(manifest: str, tool: Any) -> dict[str, Any]:
    inputs, problem = step_ports(manifest, tool)
    return {
        "name": tool.name,
        "description": str(tool.description or ""),
        "input_schema": tool_input_schema(tool),
        "inputs": None if inputs is None else [_port(p) for p in inputs],
        "problem": problem,
        "read_only": bool(getattr(tool, "read_only", False)),
        "replay_safe": bool(getattr(tool, "replay_safe", False)),
    }


@router.get(
    "/-/tools",
    response_model=StepToolList,
    responses={
        401: {"model": ManifestRefused, "description": "The manifest refuses anonymous callers"},
        403: {"model": ManifestRefused, "description": "The manifest's inbound rules refuse the caller"},
        404: {"model": ManifestRefused, "description": "No such manifest"},
        503: {"model": ManifestRefused, "description": "The manifest did not compile"},
    },
)
async def list_step_tools(
    request: Request, manifest: str = Query(min_length=1, max_length=200)
) -> dict[str, Any]:
    """The tools a `tool_call` step naming `manifest` can call, with their input schemas.

    The manifest is admitted for the caller and compiled, so this is its governed tool set as a
    run started by the caller resolves it — less client tools, which a workflow cannot answer.
    """
    require_mgmt_scopes(request, SCOPE_WORKFLOWS_READ)
    from felix.manifests.inbound_auth import InboundAuthError
    from felix.runtime import UnknownManifest

    settings = request.app.state.settings
    tenant_id = tenant_id_from_request(request)
    ctx = try_get_context()
    auth = ctx.auth if ctx is not None else AuthContext(tenant_id=tenant_id)
    try:
        async with async_run_with_context(RequestContext(settings=settings, auth=auth, manifest_id=manifest)):
            tools = await step_tools(settings, tenant_id, manifest, auth)
    except UnknownManifest:
        raise HTTPException(
            status_code=404, detail={"error": "unknown_manifest", "manifest": manifest}
        ) from None
    except InboundAuthError as refused:
        raise HTTPException(
            status_code=int(getattr(refused, "status_code", 403) or 403),
            detail={"error": "not_permitted", "manifest": manifest},
        ) from None
    except Exception:
        logger.warning(
            "workflow tools: manifest %s did not compile", loggable(manifest, limit=200), exc_info=True
        )
        raise HTTPException(
            status_code=503, detail={"error": "manifest_unavailable", "manifest": manifest}
        ) from None
    return {"manifest": manifest, "items": [_step_tool(manifest, t) for t in tools]}


@router.post("/-/check", response_model=CheckOut)
async def check_definition(spec: WorkflowSpec, request: Request) -> dict[str, Any]:
    """Ports and problems for a definition, without saving it — what the editor shows as you wire."""
    require_mgmt_scopes(request, SCOPE_WORKFLOWS_READ)
    return _checked(spec)


@router.get("", response_model=WorkflowList)
@router.get("/", response_model=WorkflowList, include_in_schema=False)
async def list_workflows(
    request: Request, limit: int = Query(default=workflow_store.MAX_LIST, ge=1, le=workflow_store.MAX_LIST)
) -> dict[str, Any]:
    """The tenant's workflows, most recently saved first, without their definitions."""
    require_mgmt_scopes(request, SCOPE_WORKFLOWS_READ)
    rows = await workflow_store.list_workflows(
        request.app.state.settings, tenant_id_from_request(request), limit=limit
    )
    return {"items": [_summary(r) for r in rows], "limit": workflow_store.MAX_PER_TENANT}


@router.get("/{workflow_id}", response_model=WorkflowOut, responses={404: _NOT_FOUND})
async def get_workflow(
    request: Request, response: Response, workflow_id: str = _WORKFLOW_ID
) -> dict[str, Any]:
    require_mgmt_scopes(request, SCOPE_WORKFLOWS_READ)
    row = await workflow_store.get_workflow(
        request.app.state.settings, tenant_id_from_request(request), workflow_id
    )
    if row is None:
        raise HTTPException(status_code=404, detail="not_found")
    response.headers["ETag"] = _etag(row)
    return _present(row)


@router.put(
    "/{workflow_id}",
    response_model=WorkflowOut,
    responses={
        201: {"model": WorkflowOut, "description": "Created"},
        409: {
            "model": WorkflowLimitResponse,
            "description": "Over the tenant's cap, or lost a race to create",
        },
        412: _CHANGED,
    },
)
async def put_workflow(
    spec: WorkflowSpec,
    request: Request,
    response: Response,
    workflow_id: str = _WORKFLOW_ID,
    if_match: str | None = Header(default=None),
    if_none_match: str | None = Header(default=None),
) -> dict[str, Any]:
    """Save a definition whole. `201` when this created it, `200` when it replaced one."""
    require_mgmt_scopes(request, SCOPE_WORKFLOWS_WRITE)
    precondition = _precondition(if_match, if_none_match)
    settings = request.app.state.settings
    tenant_id = tenant_id_from_request(request)
    who = subject_from_request(request)
    try:
        row = await workflow_store.put_workflow(
            settings,
            tenant_id,
            workflow_id,
            spec,
            precondition=precondition,
            updated_by=who,
            # Kept only if this creates it. No `authored_via`: a person's save through the routes,
            # which takes over a workflow an agent made (`spec.workflow_authoring`).
            created_by=who,
        )
    except workflow_store.WorkflowConflict as conflict:
        raise _changed(conflict, conditional=precondition is not None) from None
    except workflow_store.WorkflowLimitReached as limit:
        raise HTTPException(
            status_code=409, detail={"error": "workflow_limit", "limit": limit.limit}
        ) from None
    created = row["version"] == 1
    from felix.audit import store as audit_store

    audit_store.record_event(
        settings,
        tenant_id,
        "workflow_saved",
        principal_subj=who,
        status="ok",
        payload={"workflow_id": workflow_id, "revision": row["revision"], "created": created},
    )
    response.status_code = 201 if created else 200
    response.headers["ETag"] = _etag(row)
    return _present(row)


@router.delete("/{workflow_id}", responses={404: _NOT_FOUND, 412: _CHANGED})
async def delete_workflow(
    request: Request,
    workflow_id: str = _WORKFLOW_ID,
    if_match: str | None = Header(default=None),
) -> dict[str, str]:
    """Delete a workflow; with `If-Match`, only that revision."""
    require_mgmt_scopes(request, SCOPE_WORKFLOWS_WRITE)
    precondition = _precondition(if_match, None)
    settings = request.app.state.settings
    tenant_id = tenant_id_from_request(request)
    try:
        deleted = await workflow_store.delete_workflow(
            settings,
            tenant_id,
            workflow_id,
            precondition=precondition if isinstance(precondition, str) else None,
        )
    except workflow_store.WorkflowConflict as conflict:
        raise _changed(conflict) from None
    if not deleted:
        raise HTTPException(status_code=404, detail="not_found")
    from felix.audit import store as audit_store

    audit_store.record_event(
        settings,
        tenant_id,
        "workflow_deleted",
        principal_subj=subject_from_request(request),
        status="ok",
        payload={"workflow_id": workflow_id},
    )
    return {"status": "deleted"}
