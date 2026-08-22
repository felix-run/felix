"""Builtin tool registration shared by API compose and fiber invoke."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from felix.security.expr import evaluate_expression
from felix.tools.provider import InMemoryToolProvider, ToolProvider
from felix.tools.types import define_tool


class CalculatorArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expression: str = Field(min_length=1)


async def _calculator_handler(args: CalculatorArgs) -> str:
    try:
        return str(evaluate_expression(args.expression))
    except Exception as err:
        return f"error: {err}"


async def _list_skills_stub(_args: dict[str, Any] | None = None) -> str:
    """Fallback when builder has not bound a skill catalog."""
    return "[]"


async def _activate_skill_stub(args: dict[str, Any]) -> str:
    name = args.get("name") or args.get("skill") or ""
    return f'{{"activated":"{name}","error":"no_skill_catalog"}}'


async def _deactivate_skill_stub(args: dict[str, Any]) -> str:
    name = args.get("name") or args.get("skill") or ""
    return f'{{"deactivated":"{name}","error":"no_skill_catalog"}}'


def register_builtin_tools(provider: InMemoryToolProvider) -> None:
    """Register core local tools (calculator, workspace, skill tool names).

    Skill tools are replaced with catalog-bound implementations in
    ``build_agent`` when the manifest declares ``spec.skills`` or lists
    skill tool names.
    """
    from felix.tools.workspace import register_workspace_tools

    provider.register(
        "calculator",
        lambda: define_tool(
            name="calculator",
            description=("Evaluate a basic arithmetic expression (supports + - * / and parentheses)."),
            args=CalculatorArgs,
            handler=_calculator_handler,
        ),
    )
    register_workspace_tools(provider)
    provider.register(
        "list_skills",
        lambda: define_tool(
            name="list_skills",
            description="List available skills for this agent.",
            handler=_list_skills_stub,
        ),
    )
    provider.register(
        "activate_skill",
        lambda: define_tool(
            name="activate_skill",
            description="Activate a named skill for the current turn.",
            handler=_activate_skill_stub,
        ),
    )
    provider.register(
        "deactivate_skill",
        lambda: define_tool(
            name="deactivate_skill",
            description="Deactivate a named skill.",
            handler=_deactivate_skill_stub,
        ),
    )


def default_tool_provider() -> ToolProvider:
    """Fresh InMemoryToolProvider with builtins — used by fibers / workers."""
    provider = InMemoryToolProvider()
    register_builtin_tools(provider)
    return provider


__all__ = [
    "CalculatorArgs",
    "default_tool_provider",
    "register_builtin_tools",
]
