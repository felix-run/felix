"""Skill tools — list / activate / deactivate with progressive disclosure."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

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
            return json.dumps({"error": "unknown_skill", "name": args.name})
        active = await activation_store.activate(tenant_id, manifest_id, skill.name)
        return json.dumps(
            {
                "activated": skill.name,
                "active_skills": active,
                "instructions": skill.body or "(no body)",
            }
        )

    async def _deactivate(args: _SkillNameArgs, _ctx: ToolInvocationCtx | None = None) -> str:
        active = await activation_store.deactivate(tenant_id, manifest_id, args.name)
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
