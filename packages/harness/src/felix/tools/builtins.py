"""Builtin tool registration shared by API compose and fiber invoke."""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from felix.security.expr import evaluate_expression
from felix.tools.provider import InMemoryToolProvider, ToolProvider
from felix.tools.types import define_tool

logger = logging.getLogger("felix.tools.builtins")


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
            replay_safe=True,
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
            replay_safe=True,
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


def register_plugin_tools(provider: InMemoryToolProvider) -> None:
    """Register tools from installed plugins.

    Core names no plugin: the registry is populated by ``felix.plugins`` entry
    points. A plugin whose ``register_tools`` raises is skipped with a logged
    exception rather than taking down the caller.
    """
    from felix.plugins import get_registry, load_optional_plugins

    load_optional_plugins()
    for plugin in get_registry().plugins:
        register_tools = getattr(plugin, "register_tools", None)
        if not callable(register_tools):
            continue
        try:
            register_tools(provider.register)
        except Exception:
            logger.exception("plugin %r failed to register tools", getattr(plugin, "name", plugin))


def default_tool_provider() -> ToolProvider:
    """Fresh InMemoryToolProvider with builtins + plugin tools.

    Used by fibers, scheduled jobs, eval, and the CLI. This must include plugin
    tools: without them a manifest naming a plugin tool resolved over HTTP (which
    goes through the API's ``compose``) but raised ``Unknown tool`` when the same
    manifest ran as a durable fiber or a cron job.
    """
    provider = InMemoryToolProvider()
    register_builtin_tools(provider)
    register_plugin_tools(provider)
    return provider


__all__ = [
    "CalculatorArgs",
    "default_tool_provider",
    "register_builtin_tools",
    "register_plugin_tools",
]
