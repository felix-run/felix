"""How a workflow's parts are described to whoever builds one: the editor through `/workflows`, and
an agent through `workflow_catalog` / `check_workflow` (`agent_tools.py`).

One module, so the two cannot drift: a port attribute, a kind field, or how a plugin kind whose
config cannot be described is listed lands for both at once.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from felix.workflows.check import Problem
    from felix.workflows.ports import Port, StepPorts

logger = logging.getLogger("felix.workflows.present")


def port_view(port: Port) -> dict[str, Any]:
    from felix.workflows.ports import describe

    return {
        "name": port.name,
        "type": "control" if port.control else describe(port.schema),
        "schema": port.schema,
        "control": port.control,
        "required": port.required,
    }


def ports_view(resolved: StepPorts | None) -> dict[str, Any] | None:
    if resolved is None:
        return None
    return {
        "inputs": [port_view(p) for p in resolved.inputs],
        "outputs": [port_view(p) for p in resolved.outputs],
    }


def problem_view(problem: Problem) -> dict[str, Any]:
    return {"code": problem.code, "message": problem.message, "step": problem.step, "port": problem.port}


def step_kind_views() -> list[dict[str, Any]]:
    """The step kinds a workflow can use, built in and any a plugin registered, with each config's
    JSON Schema."""
    from felix.workflows.kinds import get_step_kind, list_step_kinds

    items = []
    for name in list_step_kinds():
        kind = get_step_kind(name)
        if kind is None:
            continue
        try:
            config_schema = kind.config.model_json_schema()
        except Exception:
            # A plugin's config model pydantic cannot describe is that kind's problem: listed with
            # no form, never a failure that empties the editor's palette or the agent's catalog.
            logger.warning("step kind %s has no JSON Schema for its config", name, exc_info=True)
            config_schema = {}
        items.append({"name": name, "label": kind.label, "config_schema": config_schema})
    return items


def step_tool_view(manifest: str, tool: Any) -> dict[str, Any]:
    """A tool a `tool_call` step naming ``manifest`` can call: its input schema, and the ports a
    step wired to it would have (or why it cannot have any)."""
    from felix.tools.types import tool_input_schema
    from felix.workflows.tool_step import step_ports

    inputs, problem = step_ports(manifest, tool)
    return {
        "name": tool.name,
        "description": str(tool.description or ""),
        "input_schema": tool_input_schema(tool),
        "inputs": None if inputs is None else [port_view(p) for p in inputs],
        "problem": problem,
        "read_only": bool(getattr(tool, "read_only", False)),
        "replay_safe": bool(getattr(tool, "replay_safe", False)),
    }


__all__ = ["port_view", "ports_view", "problem_view", "step_kind_views", "step_tool_view"]
