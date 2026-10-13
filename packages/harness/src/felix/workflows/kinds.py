"""Open step-kind registry — kinds register at import time, as patterns do.

A kind is a config model and a function from a parsed config to the step's ports. Nothing here
runs a step: execution is the run engine's, and a kind that only declares ports is enough to
draw, wire and check a workflow. A plugin adds a kind with `register_step_kind`; nothing in core
enumerates them.

The four built in are the v1 vocabulary:

- `agent_turn` runs one manifest on a prompt. Every `{{ name }}` in the prompt is a required
  input. With an `output_schema`, the turn answers in that shape and each top-level field is an
  output (plus `output`, the whole object); without one, the output is `text`.
- `tool_call` calls one tool with no model. Its inputs are the properties of the tool's input
  schema that `args` does not fix.
- `approval` parks the run until a person decides. `approved` and `denied` are control outputs,
  so what follows each is wired, not configured.
- `branch` routes on a value an earlier step produced: a boolean to `true`/`false`, or a string to
  one of its named `cases` (else `default`). There is no expression language on purpose: a step
  that needs a decision makes it upstream — an `agent_turn` whose `output_schema` has a boolean or
  an enum — and the branch only follows it.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from felix.workflows.ports import PORT_NAME_PATTERN, WHEN, Port

PortsFn = Callable[[Any], tuple[list[Port], list[Port]]]


@dataclass(frozen=True, slots=True)
class StepKind:
    # Validates a step's `config`; extra keys should be refused (`extra="forbid"`).
    config: type[BaseModel]
    # (inputs, outputs) for a parsed config. Pure: no I/O, so a workflow is checkable anywhere.
    ports: PortsFn
    # What the editor's palette calls it.
    label: str


_kinds: dict[str, StepKind] = {}


def register_step_kind(name: str, *, config: type[BaseModel], ports: PortsFn, label: str) -> None:
    _kinds[name] = StepKind(config=config, ports=ports, label=label)


def get_step_kind(name: str) -> StepKind | None:
    return _kinds.get(name)


def list_step_kinds() -> list[str]:
    return list(_kinds)


class _Config(BaseModel):
    model_config = ConfigDict(extra="forbid")


_PORT_NAME = re.compile(PORT_NAME_PATTERN)


def _check_port_name(name: str, what: str) -> str:
    if not _PORT_NAME.fullmatch(name):
        raise ValueError(f"{what} {name!r} must match {PORT_NAME_PATTERN}")
    if name == WHEN:
        raise ValueError(f"{what} {WHEN!r} is reserved for the step's control input")
    return name


# --- agent_turn -----------------------------------------------------------------------------

# `{{ name }}` with optional inner spaces. A template that needs more than substitution is a
# later decision; until then anything else between braces is literal text.
_TEMPLATE_VAR = re.compile(r"\{\{\s*([a-z][a-z0-9_]*)\s*\}\}")


class AgentTurnConfig(_Config):
    manifest: str = Field(min_length=1, max_length=200)
    prompt: str = Field(min_length=1, max_length=20_000)
    output_schema: dict[str, Any] | None = None

    @field_validator("prompt")
    @classmethod
    def _prompt_vars(cls, v: str) -> str:
        for name in _TEMPLATE_VAR.findall(v):
            _check_port_name(name, "prompt variable")
        return v

    @field_validator("output_schema")
    @classmethod
    def _output_schema(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        if v is None:
            return v
        from felix_ai.output_schema import validate_output_schema

        validate_output_schema(v)
        for name in v["properties"]:
            _check_port_name(name, "output field")
            if name == "output":
                raise ValueError("output field 'output' is the port that carries the whole object")
        return v


def render_prompt(config: AgentTurnConfig, inputs: dict[str, Any]) -> str:
    """The prompt with each `{{ name }}` replaced by its input: text as is, anything else as JSON."""
    import json

    def value(match: re.Match[str]) -> str:
        got = inputs.get(match.group(1))
        return got if isinstance(got, str) else json.dumps(got, ensure_ascii=False, default=str)

    return _TEMPLATE_VAR.sub(value, config.prompt)


def _agent_turn_ports(config: AgentTurnConfig) -> tuple[list[Port], list[Port]]:
    names = list(dict.fromkeys(_TEMPLATE_VAR.findall(config.prompt)))
    inputs = [Port(name, {}, required=True) for name in names]
    schema = config.output_schema
    if schema is None:
        return inputs, [Port("text", {"type": "string"})]
    fields = [Port(name, sub if isinstance(sub, dict) else {}) for name, sub in schema["properties"].items()]
    return inputs, [Port("output", schema), *fields]


# --- tool_call ------------------------------------------------------------------------------


class ToolCallConfig(_Config):
    tool: str = Field(min_length=1, max_length=200)
    # The tool's input JSON Schema as the harness listed it when the step was wired. A snapshot:
    # the run checks the call against the tool as it is then, and a drifted schema fails there.
    input_schema: dict[str, Any] = Field(default_factory=dict)
    # Fixed arguments. A property named here is not an input port.
    args: dict[str, Any] = Field(default_factory=dict)

    @field_validator("input_schema")
    @classmethod
    def _property_names(cls, v: dict[str, Any]) -> dict[str, Any]:
        properties = v.get("properties", {})
        if not isinstance(properties, dict):
            raise ValueError('"properties" must be an object')
        required = v.get("required", [])
        if not isinstance(required, list) or not all(isinstance(r, str) for r in required):
            raise ValueError('"required" must be a list of property names')
        for name in properties:
            _check_port_name(name, "tool input")
        return v


def _tool_call_ports(config: ToolCallConfig) -> tuple[list[Port], list[Port]]:
    properties: dict[str, Any] = config.input_schema.get("properties", {})
    required = set(config.input_schema.get("required") or [])
    inputs = [
        Port(name, sub if isinstance(sub, dict) else {}, required=name in required)
        for name, sub in properties.items()
        if name not in config.args
    ]
    return inputs, [Port("result", {})]


# --- approval -------------------------------------------------------------------------------


class ApprovalConfig(_Config):
    # What the person deciding is told, beside the `payload` the step received.
    prompt: str = Field(default="", max_length=2000)
    # How long the run waits before the request expires; the same bound an approval rule has.
    ttl_seconds: int | None = Field(default=None, ge=1, le=3600)


_DECISION = {
    "type": "object",
    "properties": {"approved": {"type": "boolean"}, "reason": {"type": "string"}},
    "required": ["approved"],
}


def _approval_ports(_config: ApprovalConfig) -> tuple[list[Port], list[Port]]:
    return (
        [Port("payload", {})],
        [Port("approved", {}, control=True), Port("denied", {}, control=True), Port("decision", _DECISION)],
    )


# --- branch ---------------------------------------------------------------------------------


class BranchConfig(_Config):
    # Empty: route a boolean to `true`/`false`. Otherwise route a string to the matching case.
    cases: list[str] = Field(default_factory=list, max_length=32)

    @field_validator("cases")
    @classmethod
    def _case_names(cls, v: list[str]) -> list[str]:
        for name in v:
            _check_port_name(name, "case")
            if name == "default":
                raise ValueError("case 'default' is the port every unmatched value takes")
        if len(set(v)) != len(v):
            raise ValueError("cases must be distinct")
        return v


def _branch_ports(config: BranchConfig) -> tuple[list[Port], list[Port]]:
    if not config.cases:
        return (
            [Port("value", {"type": "boolean"}, required=True)],
            [Port("true", {}, control=True), Port("false", {}, control=True)],
        )
    outputs = [Port(name, {}, control=True) for name in (*config.cases, "default")]
    return [Port("value", {"type": "string"}, required=True)], outputs


register_step_kind("agent_turn", config=AgentTurnConfig, ports=_agent_turn_ports, label="Agent turn")
register_step_kind("tool_call", config=ToolCallConfig, ports=_tool_call_ports, label="Tool call")
register_step_kind("approval", config=ApprovalConfig, ports=_approval_ports, label="Approval gate")
register_step_kind("branch", config=BranchConfig, ports=_branch_ports, label="Branch")


__all__ = [
    "AgentTurnConfig",
    "ApprovalConfig",
    "BranchConfig",
    "StepKind",
    "ToolCallConfig",
    "get_step_kind",
    "list_step_kinds",
    "register_step_kind",
    "render_prompt",
]
