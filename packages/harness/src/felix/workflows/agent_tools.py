"""`workflow_catalog`, `check_workflow`, `save_workflow`, `list_my_workflows` and `run_workflow`:
an agent building workflows for the person it acts for.

Bound by `manifests/builder.py` for a manifest with `spec.workflow_authoring`, before the governance
stack, so every wrapper applies -- and by default an approval rule (`workflow_save_approval_rule`)
holds each `save_workflow` call until a person has read the definition it would save.

A workflow saved here is an ordinary one (`workflows/store.py`), so the console lists, edits and
runs it like any other. What makes it this agent's is its owner: `created_by` is the person's
issuer-qualified key (`AuthContext.skill_owner` -- a bare subject is unique only within its issuer)
and `authored_via` is `agent:<manifest>`. The agent sees, changes and runs only workflows holding
both, so never an operator's or another person's; the store checks the pair under the row lock
(`put_workflow(owner=...)`), and a person's own save through the routes clears `authored_via`,
which takes the workflow over. A new workflow gets an id minted here (`wf-<hex>`), never one the
model names, so an agent cannot re-create a deleted id a job or a trigger still points at.

Saving and running refuse a run no person is in -- a scheduled firing, an inbound trigger's -- and
a run inside a workflow, so a workflow cannot write or start another. A run starts as the person
(`engine.start_run`), who must be admitted by every manifest its steps name, and the manifests a
saved workflow may name are this one unless `spec.workflow_authoring.manifests` lists more.

Every refusal is a result the model reads, `{"error": <code>, "detail": <message>}`, written here
and never an exception's text.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from felix.auth.mgmt import SCOPE_WORKFLOWS_READ, SCOPE_WORKFLOWS_WRITE
from felix.governance.permission_mode import SAVE_WORKFLOW_TOOL_NAME, WORKFLOW_SAVE_APPROVAL_RULE_ID
from felix.tools.refusal import ToolRefused, require_person
from felix.tools.types import Tool, ToolInput, ToolInvocationCtx, define_tool

if TYPE_CHECKING:
    from felix.context import AuthContext
    from felix.manifests.schema import ApprovalRule, WorkflowAuthoringSpec
    from felix.workflows.check import Problem
    from felix.workflows.ports import Port, StepPorts
    from felix.workflows.schema import WorkflowSpec

logger = logging.getLogger("felix.workflows.agent_tools")

SAVE_WORKFLOW = SAVE_WORKFLOW_TOOL_NAME
WORKFLOW_AUTHORING_TOOL_NAMES = frozenset(
    {"workflow_catalog", "check_workflow", SAVE_WORKFLOW, "list_my_workflows", "run_workflow"}
)
# What an approver sees of each agent step's prompt; the rest is counted, not shown.
PREVIEW_PROMPT_CHARS = 4000
# A run's inputs, serialized. A run's whole record is capped far above this (`runs.py`); a model
# passing more than this is passing a document, which belongs in a step's reach, not an input.
MAX_RUN_INPUT_BYTES = 64 * 1024

_DEFINITION_DESCRIPTION = (
    "The workflow definition: {name, description, inputs: {input_name: JSON Schema}, steps: "
    "[{id, kind, name, config}], edges: [{source: {step, port}, target: {step, port}}]}. The "
    "workflow's inputs are the outputs of the node `trigger`. workflow_catalog lists the step "
    "kinds, each kind's config schema and the definition's own schema."
)


class _CatalogArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manifest: str = Field(
        default="",
        description=(
            "Optional: a manifest a tool_call step may name, to list the tools it can call with "
            "their input schemas. Omit for the step kinds alone."
        ),
        json_schema_extra={"maxLength": 200},
    )


class _CheckArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    definition: dict[str, Any] = Field(description=_DEFINITION_DESCRIPTION)


class _SaveArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    definition: dict[str, Any] = Field(description=_DEFINITION_DESCRIPTION)
    workflow_id: str = Field(
        default="",
        description=(
            "Omit to create a new workflow. To replace one you saved before, its id as "
            "save_workflow or list_my_workflows gave it."
        ),
        json_schema_extra={"maxLength": 64},
    )


class _ListArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _RunArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_id: str = Field(
        min_length=1, max_length=64, description="The workflow's id, as save_workflow gave it."
    )
    inputs: dict[str, Any] = Field(
        default_factory=dict,
        description="A value for every input the workflow declares, and nothing else.",
    )
    revision: str = Field(
        default="",
        description=(
            "Optional: the revision save_workflow returned. The run is refused if the workflow "
            "has changed since, so it runs exactly what was approved."
        ),
        json_schema_extra={"maxLength": 100},
    )


def workflow_save_approval_rule() -> ApprovalRule:
    """The rule a manifest gets for `save_workflow` when `spec.workflow_authoring.approval` is on:
    single-use and for the person who asked, so an approval is never a standing licence to save."""
    from felix.manifests.schema import ApprovalRule

    return ApprovalRule(
        id=WORKFLOW_SAVE_APPROVAL_RULE_ID,
        description="Approve saving this workflow",
        tools=[SAVE_WORKFLOW],
        one_shot=True,
        bind_principal=True,
    )


def default_workflow_save_rule(
    authoring: WorkflowAuthoringSpec | None, approvals: list[ApprovalRule]
) -> ApprovalRule | None:
    """The approval rule to add for `save_workflow`, or None: as `default_scheduling_rule` decides
    for `schedule_task` -- none when authoring or its approval is off, or when one of the
    manifest's own rules names `save_workflow` literally and gates every call to it."""
    if authoring is None or not authoring.approval:
        return None
    if any(SAVE_WORKFLOW in r.tools and not r.when_args for r in approvals):
        return None
    return workflow_save_approval_rule()


def _problem(problem: Problem) -> dict[str, Any]:
    return {"code": problem.code, "message": problem.message, "step": problem.step, "port": problem.port}


def _port(port: Port) -> dict[str, Any]:
    from felix.workflows.ports import describe

    out: dict[str, Any] = {"name": port.name, "type": "control" if port.control else describe(port.schema)}
    if port.required:
        out["required"] = True
    return out


def _ports(resolved: StepPorts | None) -> dict[str, Any] | None:
    if resolved is None:
        return None
    return {"inputs": [_port(p) for p in resolved.inputs], "outputs": [_port(p) for p in resolved.outputs]}


def _parse(definition: dict[str, Any]) -> WorkflowSpec:
    """`definition` as a `WorkflowSpec`, or `invalid_definition` naming where and which rule --
    pydantic's own messages, which name the location and the rule and never the input."""
    from felix.workflows.schema import WorkflowSpec

    try:
        return WorkflowSpec.model_validate(definition)
    except ValidationError as exc:
        where = [
            f"{'.'.join(str(p) for p in err['loc']) or 'definition'}: {err['msg']}"
            for err in exc.errors(include_input=False, include_url=False)[:10]
        ]
        raise ToolRefused(
            "invalid_definition", "the definition does not parse: " + "; ".join(where)
        ) from None


def _iso(ms: Any) -> str | None:
    from datetime import UTC, datetime

    if not isinstance(ms, int):
        return None
    return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat().replace("+00:00", "Z")


class _AgentWorkflows:
    """One manifest's workflow calls, bound to its tenant and `spec.workflow_authoring`. Every
    refusal raises `ToolRefused`; the wrappers in `make_workflow_authoring_tools` return it."""

    def __init__(
        self, settings: Any, *, tenant_id: str, manifest_id: str, spec: WorkflowAuthoringSpec
    ) -> None:
        self.settings, self.tenant_id, self.manifest_id, self.spec = settings, tenant_id, manifest_id, spec
        self.via = f"agent:{manifest_id}"
        self.allowed = frozenset(spec.manifests or [manifest_id])

    def caller(
        self, scope: str, *, acting: bool, tool_ctx: ToolInvocationCtx | None = None
    ) -> tuple[AuthContext, str]:
        """The caller and the owner key their workflows carry. ``acting`` asks what only saving
        and running ask: whether a person is in this run, and whether it is inside a workflow."""
        from felix.auth.mgmt import holds_mgmt_scopes
        from felix.thread_ids import is_workflow_thread

        ctx, auth = require_person(
            no_caller=(
                "workflows built here belong to a signed-in person, and this conversation has none; "
                "nothing was saved or run"
            ),
            shared_subject=(
                "the credential this conversation uses names no person (it is shared), so a workflow "
                "could not belong to anyone; nothing was saved or run"
            ),
        )
        # The issuer-qualified key, worked out where the issuer was still known. None for a caller
        # whose sign-in names no issuer, and a bare subject could match a person at another one.
        owner = auth.skill_owner
        if not owner:
            raise ToolRefused(
                "no_owner",
                "this sign-in cannot own workflows (its issuer is not known here); nothing was saved or run",
            )
        if acting:
            if ctx.unattended:
                raise ToolRefused(
                    "unattended",
                    "this run was started by a schedule or an inbound event, not a person; workflows "
                    "are saved and run only in a conversation with one. Nothing was saved or run",
                )
            threads = (tool_ctx.thread_id if tool_ctx is not None else None, ctx.thread_id)
            if any(is_workflow_thread(self.tenant_id, t) for t in threads):
                raise ToolRefused(
                    "in_workflow",
                    "this is a step of a workflow run, which may not save or start workflows; "
                    "nothing was saved or run",
                )
        if not holds_mgmt_scopes(self.settings, auth.scopes, scope):
            raise ToolRefused("missing_scope", f"this needs the {scope} scope, which this caller lacks")
        return auth, owner

    def is_mine(self, row: dict[str, Any] | None, owner: str) -> bool:
        return row is not None and (row.get("created_by"), row.get("authored_via")) == (owner, self.via)

    async def mine(self, owner: str) -> list[dict[str, Any]]:
        """The workflows this agent holds for ``owner`` -- and nothing else in the tenant."""
        from felix.workflows import store as workflow_store

        rows = await workflow_store.list_workflows(
            self.settings, self.tenant_id, limit=workflow_store.MAX_LIST
        )
        return [r for r in rows if self.is_mine(r, owner)]

    def check_reach(self, spec: WorkflowSpec) -> list[Problem]:
        """What this agent may not save, beside what any workflow may not: too many steps, or a
        step naming a manifest outside `spec.workflow_authoring.manifests`."""
        from felix.workflows.check import Problem

        found: list[Problem] = []
        if len(spec.steps) > self.spec.max_steps:
            found.append(
                Problem(
                    "too_many_steps", f"a workflow saved here may have at most {self.spec.max_steps} steps"
                )
            )
        for step in spec.steps:
            named = step.config.get("manifest") if isinstance(step.config, dict) else None
            if isinstance(named, str) and named not in self.allowed:
                found.append(
                    Problem(
                        "manifest_not_allowed",
                        f"steps here may name only {', '.join(sorted(self.allowed))}",
                        step=step.id,
                    )
                )
        return found

    async def catalog(self, args: _CatalogArgs, _ctx: ToolInvocationCtx | None) -> str:
        from felix.workflows.kinds import get_step_kind, list_step_kinds
        from felix.workflows.schema import WorkflowSpec

        auth, _owner = self.caller(SCOPE_WORKFLOWS_READ, acting=False)
        kinds = []
        for name in list_step_kinds():
            kind = get_step_kind(name)
            if kind is None:
                continue
            try:
                config_schema = kind.config.model_json_schema()
            except Exception:
                # As the route does: a plugin's config pydantic cannot describe is listed formless.
                logger.warning("step kind %s has no JSON Schema for its config", name, exc_info=True)
                config_schema = {}
            kinds.append({"kind": name, "label": kind.label, "config_schema": config_schema})
        out: dict[str, Any] = {
            "kinds": kinds,
            "manifests": sorted(self.allowed),
            "definition_schema": WorkflowSpec.model_json_schema(),
        }
        if args.manifest:
            out["tools"] = await self.tools_of(args.manifest, auth)
        return json.dumps(out)

    async def tools_of(self, manifest: str, auth: AuthContext) -> list[dict[str, Any]]:
        """The tools a `tool_call` step naming ``manifest`` can call, as the caller would get them."""
        from felix.manifests.inbound_auth import InboundAuthError
        from felix.runtime import UnknownManifest
        from felix.tools.types import tool_input_schema
        from felix.workflows.tool_step import step_ports, step_tools

        if manifest not in self.allowed:
            raise ToolRefused(
                "manifest_not_allowed", f"steps here may name only {', '.join(sorted(self.allowed))}"
            )
        try:
            tools = await step_tools(self.settings, self.tenant_id, manifest, auth)
        except UnknownManifest:
            raise ToolRefused("unknown_manifest", "there is no manifest of that name") from None
        except InboundAuthError:
            raise ToolRefused("not_permitted", "this person may not use that manifest") from None
        except Exception:
            logger.warning("workflow_catalog: manifest did not compile", exc_info=True)
            raise ToolRefused(
                "manifest_unavailable", "that manifest could not be compiled just now"
            ) from None
        listed = []
        for tool in tools:
            if tool.name in WORKFLOW_AUTHORING_TOOL_NAMES:
                continue  # refused inside a workflow run (`caller`), so never offered to a step
            inputs, problem = step_ports(manifest, tool)
            listed.append(
                {
                    "name": tool.name,
                    "description": str(tool.description or "")[:500],
                    "input_schema": tool_input_schema(tool),
                    "inputs": None if inputs is None else [_port(p) for p in inputs],
                    "problem": problem,
                }
            )
        return listed

    async def check(self, args: _CheckArgs, _ctx: ToolInvocationCtx | None) -> str:
        from felix.workflows.engine import run_problems

        self.caller(SCOPE_WORKFLOWS_READ, acting=False)
        spec = _parse(args.definition)
        from felix.workflows.check import check as check_graph

        ports, _ = check_graph(spec)
        found = run_problems(spec) + self.check_reach(spec)
        return json.dumps(
            {
                "ok": not found,
                "problems": [_problem(p) for p in found],
                "ports": {node: _ports(p) for node, p in ports.items()},
            }
        )

    async def admit(
        self, args: _SaveArgs, tool_ctx: ToolInvocationCtx | None
    ) -> tuple[AuthContext, str, WorkflowSpec, dict[str, Any] | None]:
        """Every check `save_workflow` makes before it writes, in the order a refusal is reported:
        run by the call itself and, first, by its approval preview. Returns the row it replaces."""
        from felix.workflows import store as workflow_store
        from felix.workflows.engine import run_problems

        auth, owner = self.caller(SCOPE_WORKFLOWS_WRITE, acting=True, tool_ctx=tool_ctx)
        spec = _parse(args.definition)
        found = run_problems(spec) + self.check_reach(spec)
        if found:
            raise ToolRefused(
                "has_problems",
                "the workflow has problems and was not saved; fix them (check_workflow lists them): "
                + "; ".join(p.message for p in found[:10]),
            )
        await self.check_admitted(spec, auth)
        current = None
        if args.workflow_id:
            current = await workflow_store.get_workflow(self.settings, self.tenant_id, args.workflow_id)
            # One answer for an id that does not exist and one that is not theirs: a different one
            # would tell the model which ids other people hold.
            if not self.is_mine(current, owner):
                raise ToolRefused(
                    "no_such_workflow",
                    "there is no workflow of that id among the ones this agent holds for this person "
                    "(one a person has since saved in the console is theirs now); nothing was saved",
                )
        elif len(await self.mine(owner)) >= self.spec.max_workflows:
            raise ToolRefused(
                "too_many_workflows",
                f"this person already has {self.spec.max_workflows} workflows from this agent, the most "
                "allowed; replace one by passing its workflow_id",
            )
        return auth, owner, spec, current

    async def check_admitted(self, spec: WorkflowSpec, auth: AuthContext) -> None:
        """Each manifest a step names must admit this person, or the workflow is one they could
        not run: saving it would hand whoever starts it later steps its author could not reach."""
        from felix.manifests.inbound_auth import InboundAuthError, enforce_inbound_auth
        from felix.runtime import resolve_tenant_manifest

        named = sorted(
            {
                str(step.config["manifest"])
                for step in spec.steps
                if isinstance(step.config, dict) and isinstance(step.config.get("manifest"), str)
            }
        )
        for name in named:
            try:
                resolved = await resolve_tenant_manifest(self.settings, self.tenant_id, name)
            except Exception:
                raise ToolRefused(
                    "unknown_manifest", "a step names a manifest that does not resolve"
                ) from None
            try:
                enforce_inbound_auth(resolved.manifest, auth)
            except InboundAuthError:
                raise ToolRefused(
                    "not_permitted", "a step names a manifest this person may not use; nothing was saved"
                ) from None

    async def save(self, args: _SaveArgs, tool_ctx: ToolInvocationCtx | None) -> str:
        from felix.audit import store as audit_store
        from felix.workflows import store as workflow_store

        auth, owner, spec, current = await self.admit(args, tool_ctx)
        workflow_id = current["id"] if current is not None else f"wf-{uuid.uuid4().hex[:12]}"
        try:
            row = await workflow_store.put_workflow(
                self.settings,
                self.tenant_id,
                workflow_id,
                spec,
                # A replace lands only on the revision just checked, and only while it is still
                # this agent's -- both under the store's lock, not here.
                precondition=current["revision"] if current is not None else workflow_store.ABSENT,
                owner=(owner, self.via) if current is not None else None,
                updated_by=auth.acting_for,
                created_by=owner,
                authored_via=self.via,
            )
        except workflow_store.WorkflowNotOwned:
            raise ToolRefused(
                "no_such_workflow",
                "that workflow is no longer one this agent holds for this person (a person has saved "
                "it since); nothing was saved",
            ) from None
        except workflow_store.WorkflowConflict:
            raise ToolRefused(
                "changed", "the workflow changed while this was being saved; nothing was saved. Try again"
            ) from None
        except workflow_store.WorkflowLimitReached:
            raise ToolRefused("tenant_full", "this organisation holds the most workflows it may") from None
        created = current is None
        audit_store.record_event(
            self.settings,
            self.tenant_id,
            "workflow_saved",
            principal_subj=auth.acting_for,
            status="ok",
            payload={
                "workflow_id": workflow_id,
                "revision": row["revision"],
                "created": created,
                "via": self.via,
            },
        )
        return json.dumps(
            {
                "status": "created" if created else "replaced",
                "workflow_id": workflow_id,
                "revision": row["revision"],
                "name": spec.name,
            }
        )

    async def list_mine(self, _args: _ListArgs, _ctx: ToolInvocationCtx | None) -> str:
        from felix.workflows import runs as run_store

        _auth, owner = self.caller(SCOPE_WORKFLOWS_READ, acting=False)
        items = []
        for row in await self.mine(owner):
            definition = row.get("definition") or {}
            latest = await run_store.list_runs(self.settings, self.tenant_id, row["id"], limit=1)
            items.append(
                {
                    "workflow_id": row["id"],
                    "name": str(definition.get("name", "")),
                    "revision": row["revision"],
                    "inputs": sorted((definition.get("inputs") or {}).keys()),
                    "updated_at": _iso(row.get("updated_at")),
                    "last_run": (
                        {"run_id": latest[0]["id"], "status": latest[0]["status"]} if latest else None
                    ),
                }
            )
        return json.dumps({"workflows": items})

    async def run(self, args: _RunArgs, tool_ctx: ToolInvocationCtx | None) -> str:
        from felix.workflows import engine
        from felix.workflows import store as workflow_store

        auth, owner = self.caller(SCOPE_WORKFLOWS_WRITE, acting=True, tool_ctx=tool_ctx)
        if len(json.dumps(args.inputs, default=str).encode()) > MAX_RUN_INPUT_BYTES:
            raise ToolRefused(
                "inputs_too_large",
                f"a run's inputs may be at most {MAX_RUN_INPUT_BYTES} bytes; nothing was run",
            )
        workflow = await workflow_store.get_workflow(self.settings, self.tenant_id, args.workflow_id)
        if not self.is_mine(workflow, owner):
            raise ToolRefused(
                "no_such_workflow",
                "there is no workflow of that id among the ones this agent holds for this person; "
                "nothing was run",
            )
        assert workflow is not None  # `is_mine` is False for None
        try:
            run = await engine.start_run(
                self.settings,
                self.tenant_id,
                workflow,
                args.inputs,
                trigger=self.via,
                started_by=auth.acting_for,
                revision=args.revision or None,
                # A model's inputs may carry what it read from anyone, so every agent step of the
                # run reads them fenced as data (`kinds.render_prompt`), as a trigger's are.
                untrusted_inputs=tuple(args.inputs),
            )
        except engine.StartRefused as refused:
            # Each `StartRefused` writes its `message` for whoever asked.
            raise ToolRefused("not_started", f"{refused.message}; nothing was run") from None
        return json.dumps(
            {
                "status": run["status"],
                "run_id": run["id"],
                "workflow_id": workflow["id"],
                "revision": run["revision"],
            }
        )

    async def preview(self, args: ToolInput) -> str:
        """What an approver reads: the workflow as it would be saved, every step and edge.

        Runs `admit` first and raises its `ToolRefused`, so the approvals wrapper denies a call the
        tool would refuse with no row and no wait (`builder._approval_preview`)."""
        try:
            parsed = _SaveArgs.model_validate(args)
        except ValidationError:
            raise ToolRefused(
                "invalid_arguments", "the arguments do not match save_workflow's schema"
            ) from None
        _auth, _owner, spec, current = await self.admit(parsed, None)
        action = f"replace {current['id']}" if current is not None else "create a new workflow"
        lines = [f"save_workflow on {self.manifest_id}: {action}", f"Name: {spec.name}"]
        if spec.description:
            lines.append(f"Description: {spec.description}")
        if spec.inputs:
            lines.append("Inputs: " + ", ".join(sorted(spec.inputs)))
        lines.append("Runs later as whoever starts it: you from this agent, a job or a trigger.")
        for step in spec.steps:
            lines.append("")
            lines.append(f"--- step {step.id} ({step.kind}){f': {step.name}' if step.name else ''} ---")
            config = dict(step.config)
            prompt = config.pop("prompt", None)
            if config:
                lines.append(json.dumps(config, indent=2, sort_keys=True, default=str))
            if isinstance(prompt, str):
                shown = prompt[:PREVIEW_PROMPT_CHARS]
                more = len(prompt) - len(shown)
                lines.append("prompt:\n" + shown + (f"\n(… {more} more characters)" if more else ""))
        if spec.edges:
            lines.append("")
            lines.append("--- edges ---")
            for edge in spec.edges:
                lines.append(f"{edge.source.step}.{edge.source.port} → {edge.target.step}.{edge.target.port}")
        return "\n".join(lines)


def _answered(call: Any) -> Any:
    """A tool handler for one of `_AgentWorkflows`' calls: its refusal is the result."""

    async def handler(args: Any, ctx: ToolInvocationCtx | None = None) -> str:
        try:
            return await call(args, ctx)
        except ToolRefused as refused:
            return refused.result()

    return handler


def make_workflow_authoring_tools(
    settings: Any, *, tenant_id: str, manifest_id: str, spec: WorkflowAuthoringSpec
) -> list[Tool]:
    """The five workflow authoring tools for one manifest in one tenant."""
    flows = _AgentWorkflows(settings, tenant_id=tenant_id, manifest_id=manifest_id, spec=spec)

    catalog = define_tool(
        name="workflow_catalog",
        description=(
            "Start here to build a workflow: lists the step kinds a workflow can use with each "
            "kind's config schema, the manifests steps may name, and the definition's schema. Pass "
            "manifest to list the tools a tool_call step on it can call; copy a tool's input_schema "
            "into the step's config."
        ),
        args=_CatalogArgs,
        handler=_answered(flows.catalog),
        read_only=True,
    )
    check = define_tool(
        name="check_workflow",
        description=(
            "Check a workflow definition without saving it: every problem that would stop it "
            "running, and each step's input and output ports to wire edges between. Repeat until "
            "ok is true, then save_workflow."
        ),
        args=_CheckArgs,
        handler=_answered(flows.check),
        read_only=True,
    )
    save = define_tool(
        name=SAVE_WORKFLOW,
        description=(
            "Save a workflow for the person you are talking to, to run now or later (from this "
            "agent, the console, a job or a trigger). Omit workflow_id to create one; pass the id "
            "of one you saved to replace it. Refused while check_workflow reports problems. "
            f"One person may hold {spec.max_workflows} workflows from this agent, of at most "
            f"{spec.max_steps} steps each. Returns its id and revision."
        ),
        args=_SaveArgs,
        handler=_answered(flows.save),
    )
    save.approval_preview = flows.preview
    listing = define_tool(
        name="list_my_workflows",
        description=(
            "List the workflows you have saved for the person you are talking to: id, name, "
            "revision, inputs, and how the last run went."
        ),
        args=_ListArgs,
        handler=_answered(flows.list_mine),
        read_only=True,
    )
    run = define_tool(
        name="run_workflow",
        description=(
            "Start a run of a workflow you saved for this person, with a value for each of its "
            "inputs. It runs in the background as them; returns the run's id and status. Pass "
            "the revision save_workflow returned to run exactly what was approved."
        ),
        args=_RunArgs,
        handler=_answered(flows.run),
    )
    return [catalog, check, save, listing, run]


__all__ = [
    "SAVE_WORKFLOW",
    "WORKFLOW_AUTHORING_TOOL_NAMES",
    "default_workflow_save_rule",
    "make_workflow_authoring_tools",
    "workflow_save_approval_rule",
]
