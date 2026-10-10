"""Per-thread permission modes: how much the agent may do on its own in this conversation.

`default` runs the manifest's governance as written. `plan` lets only read-only tools run, so the
agent investigates and proposes; `exit_plan_mode` asks a person to approve the plan, and approval
returns the thread to the manifest's default mode. `accept_edits` waives approvals for the tools
that edit the workspace. `bypass` waives every approval but the plan's -- only where the manifest
allows it and for a caller holding `approvals:bypass`, checked when the mode is set *and* on every
run, so a thread left in bypass is not bypassed for the next caller who drives it.

The mode is thread state (`thread_state` meta, `permission_mode`), set by `POST /chat/mode` and by
an approved `exit_plan_mode`. What a run caches is only that stored value and the caller's bypass
verdict; each agent in the run -- a router's children, a `task` child -- resolves its *own*
effective mode from them on every call (`current_mode`), because the agents of one run need not
allow the same modes.

`plan` is honoured by every agent whether or not its manifest lists it: it only narrows what may
run. `allowed_modes` decides whether an agent offers `exit_plan_mode`, and which *widening* modes
(`accept_edits`, `bypass`) it honours; one it does not allow falls back to its `default_mode`.

Enforced in two places, both here: `apply_permission_mode`, a wrapper outside approvals (so plan
mode refuses a change before anyone is asked to approve it), and `waives_approval`, which
`apply_approvals` asks before it opens a request. Neither depends on the other having run.

The modes are a closed set (`schema.PermissionModeName`) on purpose: each says which existing
control runs, so a plugin-defined mode that switched controls off would be a governance bypass
with a name.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from felix.context import RequestContext, try_get_context
from felix.manifests.tool_match import matches_any
from felix.session.thread_state import get_thread_meta
from felix.tools.types import Tool, ToolInput, ToolInvocationCtx, ToolOutput, deny_output

if TYPE_CHECKING:
    from collections.abc import Iterable

    from felix.manifests.schema import PermissionModeName, PermissionsSpec

logger = logging.getLogger("felix.governance.permission_mode")

BYPASS_SCOPE = "approvals:bypass"
EXIT_PLAN_TOOL_NAME = "exit_plan_mode"
PLAN_APPROVAL_RULE_ID = "plan-approval"
# How long a person has to approve a plan before the request expires.
PLAN_APPROVAL_TTL_SECONDS = 1800

# The workspace tools that change files. `accept_edits` waives approvals for these and for the
# manifest's own `permissions.edit_tools`.
WORKSPACE_EDIT_TOOLS: tuple[str, ...] = ("write_file", "edit_file", "delete_file", "rename_file")

_RUN_STATE_EXTRA = "permission_mode_run_state"


@dataclass(frozen=True, slots=True)
class _RunState:
    """What one run reads once: the thread's stored mode and whether its caller may bypass."""

    stored: str | None
    may_bypass: bool


def may_bypass(settings: Any, scopes: Iterable[str]) -> bool:
    """Whether a caller holding `scopes` may use `bypass` -- the question `POST /chat/mode` and
    every run both ask (admin passes; `auth_mode=none` checks nothing, as for every management
    scope)."""
    from felix.auth.mgmt import holds_mgmt_scopes

    return holds_mgmt_scopes(settings, scopes, BYPASS_SCOPE)


async def _read_stored(req: RequestContext) -> str | None:
    if not req.thread_id:
        return None
    try:
        meta = await get_thread_meta(
            settings=req.settings, tenant_id=req.auth.tenant_id, thread_id=req.thread_id
        )
    except Exception:
        # Fail closed. An unreadable mode is read as `plan`, which only narrows: a thread that
        # was in plan mode must not leave it because a read failed, and one that was not loses
        # its writes for this run rather than gaining a waiver.
        logger.warning("permission_mode_unreadable", exc_info=True)
        return "plan"
    value = meta.get("permission_mode")
    return str(value) if value else None


async def _run_state(req: RequestContext) -> _RunState:
    cached = req.extras.get(_RUN_STATE_EXTRA)
    if isinstance(cached, _RunState):
        return cached
    state = _RunState(stored=await _read_stored(req), may_bypass=may_bypass(req.settings, req.auth.scopes))
    req.extras[_RUN_STATE_EXTRA] = state
    return state


def effective_mode(stored: str | None, spec: PermissionsSpec, *, bypass_allowed: bool) -> PermissionModeName:
    """The mode one agent is held to, given the thread's stored mode."""
    if stored == "plan":
        return "plan"
    if stored == "accept_edits" and "accept_edits" in spec.allowed_modes:
        return "accept_edits"
    if stored == "bypass" and "bypass" in spec.allowed_modes and bypass_allowed:
        return "bypass"
    return spec.default_mode


async def current_mode(spec: PermissionsSpec, req: RequestContext | None) -> PermissionModeName:
    if req is None:
        return spec.default_mode
    state = await _run_state(req)
    return effective_mode(state.stored, spec, bypass_allowed=state.may_bypass)


def _is_read_only(tool: Tool, spec: PermissionsSpec) -> bool:
    return tool.read_only or tool.name == EXIT_PLAN_TOOL_NAME or matches_any(spec.read_only_tools, tool.name)


def _is_edit(tool_name: str, spec: PermissionsSpec) -> bool:
    return tool_name in WORKSPACE_EDIT_TOOLS or matches_any(spec.edit_tools, tool_name)


async def waives_approval(
    tool_name: str, spec: PermissionsSpec | None, req: RequestContext | None
) -> str | None:
    """The mode that waives the approval `tool_name` would otherwise ask for, or None."""
    if spec is None or req is None or tool_name == EXIT_PLAN_TOOL_NAME:
        # The plan approval is the one no mode waives: it is how plan mode ends.
        return None
    mode = await current_mode(spec, req)
    if mode == "bypass" or (mode == "accept_edits" and _is_edit(tool_name, spec)):
        return mode
    return None


def record_waiver(req: RequestContext, tool_name: str, mode: str, manifest_id: str) -> None:
    """Every approval a mode waived is an audit row: the trail of what ran without asking."""
    from felix.audit import store as audit_store
    from felix.observability.metrics import record_counter

    record_counter("felix_approval_waived", {"manifest_id": manifest_id, "mode": mode})
    audit_store.record_event(
        req.settings,
        req.auth.tenant_id,
        "approval_waived",
        principal_subj=req.auth.on_behalf_of or req.auth.principal_sub or "",
        status=mode,
        payload={"tool": tool_name, "thread_id": req.thread_id or "", "manifest_id": manifest_id},
    )


def apply_permission_mode(tools: list[Tool], spec: PermissionsSpec, manifest_id: str) -> list[Tool]:
    """Refuse every tool that is not read-only while this agent is in plan mode."""
    from felix.tools.executor import wrap_executor

    way_out = (
        f"this conversation is in plan mode: investigate with read-only tools, then propose the plan "
        f"with {EXIT_PLAN_TOOL_NAME}"
        if "plan" in spec.allowed_modes
        else "the conversation was put in plan mode, which this agent cannot leave: ask the person "
        "driving it to change the mode"
    )

    def wrap_one(tool: Tool) -> Tool:
        if _is_read_only(tool, spec):
            return tool
        inner = tool.executor

        async def execute(args: ToolInput, ctx: ToolInvocationCtx | None = None) -> ToolOutput:
            if await current_mode(spec, try_get_context()) == "plan":
                return deny_output(
                    f"[plan mode] {tool.name} can change things, and {way_out}", "permission_mode"
                )
            return await inner.execute(args, ctx)

        return replace(tool, executor=wrap_executor(inner, execute))

    return [wrap_one(t) for t in tools]


def make_exit_plan_tool(spec: PermissionsSpec) -> Tool:
    """`exit_plan_mode(plan)`: gated by the `plan-approval` rule the builder adds, so this runs only
    once a person approved the plan -- and then returns the thread to the default mode."""
    from felix.tools.errors import ToolErrorCode, tool_error_output
    from felix.tools.types import define_tool

    async def handler(args: dict[str, Any]) -> ToolOutput:
        req = try_get_context()
        if req is None:
            return tool_error_output(ToolErrorCode.INTERNAL, f"[{EXIT_PLAN_TOOL_NAME}] no request context")
        if await current_mode(spec, req) != "plan":
            return f"[{EXIT_PLAN_TOOL_NAME}] this conversation is not in plan mode; carry on"
        await set_thread_mode(req, spec.default_mode, via=EXIT_PLAN_TOOL_NAME)
        return f"Plan approved. Plan mode is off ({spec.default_mode}); carry it out."

    async def bound_to_thread(_args: ToolInput) -> str:
        # Joins the plan text in the grant's signature: approving a plan on one thread
        # authorizes nothing on another, however alike the plans read.
        req = try_get_context()
        return (req.thread_id or "") if req else ""

    tool = define_tool(
        name=EXIT_PLAN_TOOL_NAME,
        description=(
            "In plan mode, present your finished plan for approval. A person reviews it; once "
            "approved, plan mode ends and you can carry the plan out. Use it only when the plan "
            "is complete and concrete."
        ),
        args_schema={
            "type": "object",
            "properties": {
                "plan": {"type": "string", "minLength": 1, "description": "The plan, in Markdown."},
            },
            "required": ["plan"],
            "additionalProperties": False,
        },
        handler=handler,
        read_only=True,
    )
    return replace(tool, approval_binding=bound_to_thread)


def plan_approval_rule() -> Any:
    """The rule gating `exit_plan_mode`: single-use, for the person who asked, and short-lived,
    so an approved plan is never a standing key out of plan mode."""
    from felix.manifests.schema import ApprovalRule

    return ApprovalRule(
        id=PLAN_APPROVAL_RULE_ID,
        description="Approve this plan and leave plan mode",
        tools=[EXIT_PLAN_TOOL_NAME],
        one_shot=True,
        bind_principal=True,
        ttl_seconds=PLAN_APPROVAL_TTL_SECONDS,
    )


def record_mode_change(
    settings: Any, tenant_id: str, principal: str, thread_id: str, mode: str, *, via: str
) -> None:
    from felix.audit import store as audit_store

    audit_store.record_event(
        settings,
        tenant_id,
        "permission_mode_change",
        principal_subj=principal,
        status=mode,
        payload={"thread_id": thread_id, "mode": mode, "via": via},
    )


async def set_thread_mode(req: RequestContext, mode: str, *, via: str) -> None:
    """Store `mode` on the run's thread, audit and announce it, and use it for the rest of the run."""
    from felix.session.thread_state import update_thread_meta
    from felix.side_events import emit as emit_side_event

    if req.thread_id:
        await update_thread_meta(
            settings=req.settings, tenant_id=req.auth.tenant_id, thread_id=req.thread_id, permission_mode=mode
        )
    state = await _run_state(req)
    req.extras[_RUN_STATE_EXTRA] = _RunState(stored=mode, may_bypass=state.may_bypass)
    record_mode_change(
        req.settings,
        req.auth.tenant_id,
        req.auth.on_behalf_of or req.auth.principal_sub or "",
        req.thread_id or "",
        mode,
        via=via,
    )
    await emit_side_event(req.thread_id, "permission_mode_changed", {"mode": mode})


async def in_plan_mode(req: RequestContext) -> bool:
    """Whether this run's thread is in plan mode -- for a tool, like `task`, that must not start
    work outside the run. Every agent honours a stored `plan`, so the stored value answers it."""
    return (await _run_state(req)).stored == "plan"


__all__ = [
    "BYPASS_SCOPE",
    "EXIT_PLAN_TOOL_NAME",
    "PLAN_APPROVAL_RULE_ID",
    "WORKSPACE_EDIT_TOOLS",
    "apply_permission_mode",
    "current_mode",
    "effective_mode",
    "in_plan_mode",
    "make_exit_plan_tool",
    "may_bypass",
    "plan_approval_rule",
    "record_mode_change",
    "record_waiver",
    "set_thread_mode",
    "waives_approval",
]
