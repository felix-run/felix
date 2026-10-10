"""Per-thread permission modes: how much the agent may do on its own in this conversation.

`default` runs the manifest's governance as written. `plan` lets only read-only tools run, so the
agent investigates and proposes; `exit_plan_mode` asks a person to approve the plan, and approval
returns the thread to the manifest's default mode. `accept_edits` waives approvals for the tools
that edit the workspace. `bypass` waives every approval -- only where the manifest allows it and
for a caller holding `approvals:bypass`, checked when the mode is set *and* on every turn, so a
thread left in bypass is not bypassed for the next caller who drives it.

The mode is thread state (`thread_state` meta, `permission_mode`), set by `POST /chat/mode` and by
an approved `exit_plan_mode`, and read once per run onto the request context. It is enforced in
two places, both here: `apply_permission_mode`, a wrapper outside approvals (so plan mode refuses
a change before anyone is asked to approve it), and `waives_approval`, which `apply_approvals`
asks before it opens a request.

The four modes are a closed set on purpose. They are not swappable implementations behind a
registry; each is a statement about which existing control runs, and a plugin-defined mode that
switched controls off would be a governance bypass with a name.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Literal

from felix.context import RequestContext, try_get_context
from felix.manifests.tool_match import matches_any
from felix.tools.types import Tool, ToolInput, ToolInvocationCtx, ToolOutput, deny_output

if TYPE_CHECKING:
    from felix.manifests.schema import PermissionsSpec

logger = logging.getLogger("felix.governance.permission_mode")

PermissionMode = Literal["default", "accept_edits", "plan", "bypass"]
PERMISSION_MODES: tuple[str, ...] = ("default", "accept_edits", "plan", "bypass")
BYPASS_SCOPE = "approvals:bypass"
EXIT_PLAN_TOOL_NAME = "exit_plan_mode"

# The workspace tools that change files. `accept_edits` waives approvals for these and for the
# manifest's own `permissions.edit_tools`.
WORKSPACE_EDIT_TOOLS: tuple[str, ...] = ("write_file", "edit_file", "delete_file", "rename_file")

# The run's resolved mode, cached on the request context by the first wrapped call to ask.
_EXTRA = "permission_mode"


def may_bypass(req: RequestContext) -> bool:
    """Whether this run's caller holds `approvals:bypass` (admin passes; `auth_mode=none` checks
    nothing, as for every management scope)."""
    from felix.auth.mgmt import holds_mgmt_scopes

    return holds_mgmt_scopes(req.settings, req.auth.scopes, BYPASS_SCOPE)


async def stored_mode(req: RequestContext) -> str | None:
    if not req.thread_id:
        return None
    from felix.session.thread_state import get_thread_meta

    try:
        meta = await get_thread_meta(
            settings=req.settings, tenant_id=req.auth.tenant_id, thread_id=req.thread_id
        )
    except Exception:
        # Fail toward the manifest's default, never toward a waiver: an unreadable mode is
        # treated as unset, and `default` runs every control as written.
        logger.warning("permission_mode_unreadable", exc_info=True)
        return None
    value = meta.get("permission_mode")
    return str(value) if value else None


def effective_mode(stored: str | None, spec: PermissionsSpec, req: RequestContext) -> str:
    """The mode this run is held to: the stored one if the manifest allows it and -- for
    `bypass` -- the caller may use it, else the manifest's default."""
    mode = stored if stored in spec.allowed_modes else spec.default_mode
    if mode == "bypass" and not may_bypass(req):
        return spec.default_mode
    return mode


async def current_mode(spec: PermissionsSpec, req: RequestContext | None) -> str:
    if req is None:
        return spec.default_mode
    cached = req.extras.get(_EXTRA)
    if isinstance(cached, str):
        return cached
    mode = effective_mode(await stored_mode(req), spec, req)
    req.extras[_EXTRA] = mode
    return mode


def run_mode(req: RequestContext) -> str | None:
    """The mode this run resolved, or None if no wrapped tool has asked yet."""
    mode = req.extras.get(_EXTRA)
    return mode if isinstance(mode, str) else None


def set_run_mode(req: RequestContext, mode: str) -> None:
    """Change the mode for the rest of this run, after the thread's stored mode was changed."""
    req.extras[_EXTRA] = mode


def _is_read_only(tool: Tool, spec: PermissionsSpec) -> bool:
    return tool.read_only or tool.name == EXIT_PLAN_TOOL_NAME or matches_any(spec.read_only_tools, tool.name)


def _is_edit(tool_name: str, spec: PermissionsSpec) -> bool:
    return tool_name in WORKSPACE_EDIT_TOOLS or matches_any(spec.edit_tools, tool_name)


def waives_approval(tool_name: str, spec: PermissionsSpec | None, req: RequestContext | None) -> bool:
    """Whether this run's mode skips the approval `tool_name` would otherwise ask for.

    Read from the run's cached mode, which the permission-mode wrapper -- outside approvals --
    resolved before this call reached approvals. With no cached mode nothing is waived.
    """
    if spec is None or req is None or tool_name == EXIT_PLAN_TOOL_NAME:
        # The plan approval is the one approval no mode waives: it is how plan mode ends.
        return False
    mode = run_mode(req)
    # A child agent shares its parent's run, and so its resolved mode. Plan mode only narrows,
    # so it applies to the child as it is; a waiver applies only where the child's *own*
    # manifest allows that mode -- a parent in bypass does not waive a child's approvals.
    if mode not in spec.allowed_modes:
        return False
    if mode == "bypass":
        return True
    return mode == "accept_edits" and _is_edit(tool_name, spec)


def apply_permission_mode(tools: list[Tool], spec: PermissionsSpec, manifest_id: str) -> list[Tool]:
    """Refuse every tool that is not read-only while the thread is in plan mode."""
    from felix.manifests.builder import _clone_tool
    from felix.tools.executor import wrap_executor

    def wrap_one(tool: Tool) -> Tool:
        if _is_read_only(tool, spec):
            # Still resolves the mode, so `waives_approval` downstream has it for this call.
            inner_ro = tool.executor

            async def execute_ro(args: ToolInput, ctx: ToolInvocationCtx | None = None) -> ToolOutput:
                await current_mode(spec, try_get_context())
                return await inner_ro.execute(args, ctx)

            return _clone_tool(tool, wrap_executor(inner_ro, execute_ro))
        inner = tool.executor

        async def execute(args: ToolInput, ctx: ToolInvocationCtx | None = None) -> ToolOutput:
            if await current_mode(spec, try_get_context()) == "plan":
                return deny_output(
                    f"[plan mode] {tool.name} can change things, and this conversation is in plan "
                    f"mode: investigate with read-only tools, then propose the plan with "
                    f"{EXIT_PLAN_TOOL_NAME}",
                    "permission_mode",
                )
            return await inner.execute(args, ctx)

        return _clone_tool(tool, wrap_executor(inner, execute))

    return [wrap_one(t) for t in tools]


def make_exit_plan_tool(spec: PermissionsSpec) -> Tool:
    """`exit_plan_mode(plan)`: gated by the approval rule the builder adds for it, so this runs
    only once a person approved the plan -- and then returns the thread to the default mode."""
    from felix.tools.errors import ToolErrorCode, tool_error_output
    from felix.tools.types import define_tool

    async def handler(args: dict[str, Any]) -> ToolOutput:
        req = try_get_context()
        if req is None:
            return tool_error_output(ToolErrorCode.INTERNAL, f"[{EXIT_PLAN_TOOL_NAME}] no request context")
        if await current_mode(spec, req) != "plan":
            return f"[{EXIT_PLAN_TOOL_NAME}] this conversation is not in plan mode; carry on"
        await set_thread_mode(req, spec.default_mode)
        return f"Plan approved. Plan mode is off ({spec.default_mode}); carry it out."

    return define_tool(
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


async def set_thread_mode(req: RequestContext, mode: str) -> None:
    """Store `mode` on the run's thread, announce it, and switch the rest of this run to it."""
    from felix.session.thread_state import update_thread_meta
    from felix.side_events import emit as emit_side_event

    if req.thread_id:
        await update_thread_meta(
            settings=req.settings, tenant_id=req.auth.tenant_id, thread_id=req.thread_id, permission_mode=mode
        )
    set_run_mode(req, mode)
    await emit_side_event(req.thread_id, "permission_mode_changed", {"mode": mode})


__all__ = [
    "BYPASS_SCOPE",
    "EXIT_PLAN_TOOL_NAME",
    "PERMISSION_MODES",
    "WORKSPACE_EDIT_TOOLS",
    "PermissionMode",
    "apply_permission_mode",
    "current_mode",
    "effective_mode",
    "make_exit_plan_tool",
    "may_bypass",
    "run_mode",
    "set_thread_mode",
    "waives_approval",
]
