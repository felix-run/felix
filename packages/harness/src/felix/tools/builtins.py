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


async def _list_skills(_args: dict[str, Any] | None = None) -> str:
    return "[]"


async def _activate_skill(args: dict[str, Any]) -> str:
    return f"activated:{args.get('name') or args.get('skill') or ''}"


async def _deactivate_skill(args: dict[str, Any]) -> str:
    return f"deactivated:{args.get('name') or args.get('skill') or ''}"


def register_builtin_tools(provider: InMemoryToolProvider) -> None:
    """Register core local tools (calculator + skill placeholders)."""
    provider.register(
        "calculator",
        lambda: define_tool(
            name="calculator",
            description=(
                "Evaluate a basic arithmetic expression (supports + - * / and parentheses)."
            ),
            args=CalculatorArgs,
            handler=_calculator_handler,
        ),
    )
    provider.register(
        "list_skills",
        lambda: define_tool(
            name="list_skills",
            description="List available skills for this agent.",
            handler=_list_skills,
        ),
    )
    provider.register(
        "activate_skill",
        lambda: define_tool(
            name="activate_skill",
            description="Activate a named skill for the current turn.",
            handler=_activate_skill,
        ),
    )
    provider.register(
        "deactivate_skill",
        lambda: define_tool(
            name="deactivate_skill",
            description="Deactivate a named skill.",
            handler=_deactivate_skill,
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
