"""Skill tools — list / activate / deactivate with progressive disclosure."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from felix.audit.emit import emit_agent_audit
from felix.logging_setup import loggable
from felix.skills.store import SkillActivationStore
from felix.skills.types import SkillCatalog
from felix.tools.types import Tool, ToolInvocationCtx, define_tool


class _SkillNameArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, description="Skill name to activate or deactivate.")


def make_skill_tools(
    catalog: SkillCatalog,
    *,
    activation_store: SkillActivationStore,
    tenant_id: str,
    manifest_id: str,
) -> list[Tool]:
    """Build list_skills / activate_skill / deactivate_skill bound to a catalog."""

    def _audit(action: str, skill: str, *, status: str, ctx: ToolInvocationCtx | None) -> None:
        """Name the skill in the trail, which the generic tool-call event cannot.

        `tool_runner` already emits a `tool_call` event for every tool, but its payload
        carries the tool's *name* and not its arguments -- so the trail recorded that a
        skill was activated and never which one. Deliberately: a tool's arguments are
        arbitrary caller and model text, and putting them in the audit payload wholesale is
        how a credential ends up in a retained row.

        A skill name is the exception worth making, and only because it is not arbitrary:
        both tools resolve it against the catalog first, so a row carrying `status="ok"`
        holds a name the host declared rather than anything the model typed. The
        `unknown_skill` arms are the only places a model-supplied string is recorded, which
        is why they are bounded below and marked with their own status -- a model probing
        for skills it has not been granted is itself the thing an operator wants to see.

        `tool_call_id` rides along because `tool_runner` writes it on the `tool_call` event
        for the same invocation, and it is the only thing that can join the two. Parallel
        tool calls in one batch produce rows with the same `thread_id` and adjacent `ts`;
        without this there is nothing to tell them apart. Audit payloads are the hardest
        thing here to change later -- rows are retained under a manifest TTL and fanned out
        to an operator's sink on write -- so the key goes in now or never.
        """
        emit_agent_audit(
            "skill_activation",
            status=status,
            manifest_id=manifest_id,
            payload={
                "action": action,
                "skill": loggable(skill, limit=64),
                "thread_id": getattr(ctx, "thread_id", None) or "",
                "tool_call_id": getattr(ctx, "tool_call_id", None) or "",
            },
        )

    async def _list(_args: dict[str, Any] | None = None, _ctx: ToolInvocationCtx | None = None) -> str:
        active = await activation_store.get_active(tenant_id, manifest_id)
        payload = [
            {
                "name": s.name,
                "description": s.description,
                "active": s.name in active,
                "has_body": bool(s.body),
            }
            for s in catalog.list_public()
        ]
        # Also surface disable_model_invocation skills as inactive-only via list? skip per spec.
        return json.dumps(payload)

    async def _activate(args: _SkillNameArgs, _ctx: ToolInvocationCtx | None = None) -> str:
        skill = catalog.get(args.name)
        if skill is None:
            _audit("activate", args.name, status="unknown_skill", ctx=_ctx)
            return json.dumps({"error": "unknown_skill", "name": args.name})
        active = await activation_store.activate(tenant_id, manifest_id, skill.name)
        _audit("activate", skill.name, status="ok", ctx=_ctx)
        return json.dumps(
            {
                "activated": skill.name,
                "active_skills": active,
                "instructions": skill.body or "(no body)",
            }
        )

    async def _deactivate(args: _SkillNameArgs, _ctx: ToolInvocationCtx | None = None) -> str:
        # Resolved first, like `activate`. `activation_store.deactivate` is a list filter --
        # it neither validates the name nor reports whether anything was removed -- so
        # auditing the raw argument recorded a model-supplied string under `status="ok"`,
        # indistinguishable from a deactivation that happened.
        skill = catalog.get(args.name)
        active = await activation_store.deactivate(tenant_id, manifest_id, args.name)
        if skill is None:
            _audit("deactivate", args.name, status="unknown_skill", ctx=_ctx)
        else:
            _audit("deactivate", skill.name, status="ok", ctx=_ctx)
        return json.dumps({"deactivated": args.name, "active_skills": active})

    return [
        define_tool(
            name="list_skills",
            description="List available skills for this agent (name, description, active).",
            handler=_list,
        ),
        define_tool(
            name="activate_skill",
            description=(
                "Activate a named skill and return its full instructions. "
                "Call when a task matches a skill description."
            ),
            args=_SkillNameArgs,
            handler=_activate,
        ),
        define_tool(
            name="deactivate_skill",
            description="Deactivate a named skill for this agent.",
            args=_SkillNameArgs,
            handler=_deactivate,
        ),
    ]


SKILL_TOOL_NAMES = frozenset({"list_skills", "activate_skill", "deactivate_skill"})

__all__ = ["SKILL_TOOL_NAMES", "make_skill_tools"]
