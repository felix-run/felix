"""The authored workflow definition: typed steps wired port to port.

A workflow is a DAG. Each step is an instance of a *step kind* (`kinds.py`, an open registry),
and its ports — named, each typed by a JSON Schema — come from the kind and the step's config.
An edge joins one step's output port to another step's input port: the data rides on the edge,
and the edge is also the ordering. A *control* port carries no data, only "this way was taken":
an approval gate's `approved` and `denied`, a branch's cases. Every step has an implicit control
input, `when`; a step with an edge into it runs only if one of those edges fired.

The workflow's own inputs are the outputs of a node that is not a step, `trigger`: what a manual
run's form, a job's payload or an inbound event supplies.

Structure is validated on parse (`WorkflowSpec`). The graph is checked by `problems()`, which
returns rather than raises, because the editor saves half-wired drafts: a workflow with problems
can be stored, and only a run refuses one.

A step's `kind` is an open `str` and its `config` a plain dict, validated by the kind in
`problems()` rather than on parse. Both are deliberate. A stored workflow naming a kind whose
plugin was later uninstalled, or a config a kind later tightened, must still load so the editor
can say what is wrong with it; a stored row that stopped parsing is the outage
`manifests/compat.py` documents for manifests.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from felix.logging_setup import loggable

logger = logging.getLogger("felix.workflows.schema")

# A workflow id is a URL path segment; a step id and a port name are referenced from edges and,
# later, from templates, so all three keep to one conservative alphabet.
WORKFLOW_ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"
STEP_ID_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
PORT_NAME_PATTERN = STEP_ID_PATTERN
_PORT_NAME = re.compile(PORT_NAME_PATTERN)

# The node whose outputs are the workflow's inputs. Not a step, so no step may take its id.
TRIGGER = "trigger"
# The implicit control input every step has.
WHEN = "when"

MAX_STEPS = 200
MAX_EDGES = 1000


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Position(_Strict):
    """Where the editor drew a node. Saved with the definition so a reload does not re-lay it."""

    x: float
    y: float


class PortRef(_Strict):
    step: str = Field(min_length=1, max_length=64)
    port: str = Field(min_length=1, max_length=64)


class Edge(_Strict):
    """An output port (or a workflow input, on `trigger`) wired to an input port."""

    source: PortRef
    target: PortRef


class Step(_Strict):
    id: str = Field(pattern=STEP_ID_PATTERN)
    # Open on purpose: see the module docstring.
    kind: str = Field(min_length=1, max_length=64)
    name: str = Field(default="", max_length=200)
    config: dict[str, Any] = Field(default_factory=dict)
    position: Position | None = None


class WorkflowSpec(_Strict):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    # Workflow input name → JSON Schema (`{}` accepts anything): the `trigger` node's outputs.
    inputs: dict[str, dict[str, Any]] = Field(default_factory=dict)
    steps: list[Step] = Field(default_factory=list, max_length=MAX_STEPS)
    edges: list[Edge] = Field(default_factory=list, max_length=MAX_EDGES)
    trigger_position: Position | None = None

    @field_validator("inputs")
    @classmethod
    def _input_names(cls, v: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        for name in v:
            if not _PORT_NAME.match(name):
                raise ValueError(f"input name {name!r} must match {PORT_NAME_PATTERN}")
        return v

    @model_validator(mode="after")
    def _step_ids(self) -> WorkflowSpec:
        seen: set[str] = set()
        for step in self.steps:
            if step.id == TRIGGER:
                raise ValueError(f"step id {TRIGGER!r} is reserved for the workflow's inputs")
            if step.id in seen:
                raise ValueError(f"step id {step.id!r} is used twice")
            seen.add(step.id)
        return self


@dataclass(frozen=True, slots=True)
class Port:
    name: str
    # JSON Schema of the value; `{}` accepts or produces anything. Ignored on a control port.
    schema: dict[str, Any]
    control: bool = False
    # An input the step cannot run without. Meaningless on an output.
    required: bool = False


@dataclass(frozen=True, slots=True)
class Problem:
    """One thing that stops a workflow running. `code` is stable; `message` is for a person."""

    code: str
    message: str
    step: str | None = None
    port: str | None = None


# --- port types -----------------------------------------------------------------------------


def _types(schema: dict[str, Any]) -> set[str]:
    declared = schema.get("type")
    if isinstance(declared, str):
        return {declared}
    if isinstance(declared, list):
        return {t for t in declared if isinstance(t, str)}
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return {_json_type(v) for v in enum}
    return set()


def _json_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if value is None:
        return "null"
    return "array" if isinstance(value, list) else "object"


# Deeper than this, two schemas are taken as compatible: the check is advisory, and a recursive
# walk over an author's arbitrarily nested schema must not become a `RecursionError`.
_MAX_TYPE_DEPTH = 16


def describe(schema: dict[str, Any], _depth: int = 0) -> str:
    """A port type as the editor shows it: `string`, `array<object>`, `enum<bug|feature>`.

    Nesting past `_MAX_TYPE_DEPTH` is shown as `…`: this runs on an author's schema, inside the
    mismatch message, and must not recurse as deep as the schema does.
    """
    if _depth > _MAX_TYPE_DEPTH:
        return "…"
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return "enum<" + "|".join(str(v) for v in enum) + ">"
    types = _types(schema)
    if not types:
        return "any"
    if types == {"array"}:
        items = schema.get("items")
        return f"array<{describe(items, _depth + 1) if isinstance(items, dict) else 'any'}>"
    return "|".join(sorted(types))


def _schema_mismatch(source: dict[str, Any], target: dict[str, Any], depth: int = 0) -> str | None:
    if not source or not target or depth > _MAX_TYPE_DEPTH:
        return None
    src_types, dst_types = _types(source), _types(target)
    if src_types and dst_types:
        accepted = dst_types | ({"integer"} if "number" in dst_types else set())
        if not src_types <= accepted:
            return f"{describe(source)} → {describe(target)}"
    if _enum_narrows(source, target):
        return f"{describe(source)} → {describe(target)}"
    if src_types == {"array"} and dst_types == {"array"}:
        src_items, dst_items = source.get("items"), target.get("items")
        if (
            isinstance(src_items, dict)
            and isinstance(dst_items, dict)
            and _schema_mismatch(src_items, dst_items, depth + 1) is not None
        ):
            return f"{describe(source)} → {describe(target)}"
    if src_types == {"object"} and dst_types == {"object"}:
        return _object_mismatch(source, target, depth)
    return None


def _enum_narrows(source: dict[str, Any], target: dict[str, Any]) -> bool:
    """The target takes only listed values and the source may produce one it does not list."""
    dst_enum = target.get("enum")
    if not isinstance(dst_enum, list) or not dst_enum:
        return False
    src_enum = source.get("enum")
    return not isinstance(src_enum, list) or not set(map(repr, src_enum)) <= set(map(repr, dst_enum))


def _object_mismatch(source: dict[str, Any], target: dict[str, Any], depth: int) -> str | None:
    """A field the target requires that the source does not declare, or declares as another type.

    A source that declares no `properties` at all is an object of unknown shape and passes.
    """
    src_props = source.get("properties")
    if not isinstance(src_props, dict):
        return None
    dst_props = target.get("properties")
    if not isinstance(dst_props, dict):
        dst_props = {}
    required = target.get("required")
    for name in required if isinstance(required, list) else []:
        if not isinstance(name, str):
            continue
        if name not in src_props:
            return f"{describe(source)} has no field {name!r}"
        src_field, dst_field = src_props[name], dst_props.get(name)
        if isinstance(src_field, dict) and isinstance(dst_field, dict):
            inner = _schema_mismatch(src_field, dst_field, depth + 1)
            if inner is not None:
                return f"field {name!r}: {inner}"
    return None


def port_mismatch(source: Port, target: Port) -> str | None:
    """Why `source` cannot feed `target`, or `None` when it can.

    Deliberately partial: it compares the parts of JSON Schema a port type is drawn from
    (`type`, `enum`, `items`, `properties`/`required`) and accepts whatever it cannot judge,
    because a refused edge the author knows is fine is worse than a run that fails on a value
    the harness then reports exactly.
    """
    if source.control != target.control:
        return (
            "a control port can only feed a control port"
            if source.control
            else ("a data port cannot feed a control port")
        )
    if source.control:
        return None
    return _schema_mismatch(source.schema, target.schema)


# --- the graph ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StepPorts:
    inputs: tuple[Port, ...]
    outputs: tuple[Port, ...]


def _kind_error(step: Step, doing: str) -> Problem:
    # A plugin kind is third-party code running on an author's config; one that raises is that
    # step's problem, never a failure of the whole check, which would make a stored draft
    # unopenable. Pydantic turns only `ValueError` and `AssertionError` into validation errors.
    logger.warning(
        "step kind %s raised %s step %s",
        loggable(step.kind, limit=64),
        doing,
        loggable(step.id, limit=64),
        exc_info=True,
    )
    return Problem("kind_error", f"step kind {step.kind!r} failed on this step", step=step.id)


def resolve_ports(step: Step) -> tuple[StepPorts | None, list[Problem]]:
    """A step's ports from its kind and config, or the problems that stop them being known."""
    from felix.workflows.kinds import get_step_kind

    kind = get_step_kind(step.kind)
    if kind is None:
        return None, [Problem("unknown_kind", f"no step kind named {step.kind!r}", step=step.id)]
    try:
        config = kind.config.model_validate(step.config)
    except ValidationError as exc:
        return None, [
            Problem(
                "invalid_config",
                f"{'.'.join(str(p) for p in err['loc']) or 'config'}: {err['msg']}",
                step=step.id,
            )
            for err in exc.errors()
        ]
    except Exception:
        return None, [_kind_error(step, "validating")]
    try:
        inputs, outputs = kind.ports(config)
    except Exception:
        return None, [_kind_error(step, "resolving ports for")]
    return StepPorts(inputs=(*inputs, Port(WHEN, {}, control=True)), outputs=tuple(outputs)), []


def problems(spec: WorkflowSpec) -> list[Problem]:
    """Everything that stops `spec` running, in a stable order; empty when it can run."""
    found: list[Problem] = []
    ports: dict[str, StepPorts | None] = {
        TRIGGER: StepPorts(inputs=(), outputs=tuple(Port(n, s) for n, s in spec.inputs.items()))
    }
    for step in spec.steps:
        resolved, step_problems = resolve_ports(step)
        ports[step.id] = resolved
        found.extend(step_problems)

    fed: dict[tuple[str, str], int] = {}
    graph: dict[str, set[str]] = {step.id: set() for step in spec.steps}
    for edge in spec.edges:
        src, dst = edge.source, edge.target
        if src.step not in ports:
            found.append(Problem("unknown_step", f"no step {src.step!r}", step=dst.step, port=dst.port))
            continue
        if dst.step == TRIGGER or dst.step not in ports:
            found.append(Problem("unknown_step", f"no step {dst.step!r} to wire into", step=src.step))
            continue
        if src.step != TRIGGER:
            graph[src.step].add(dst.step)
        src_ports, dst_ports = ports[src.step], ports[dst.step]
        if src_ports is None or dst_ports is None:
            continue  # already reported: the step's kind or config is the problem
        out = next((p for p in src_ports.outputs if p.name == src.port), None)
        into = next((p for p in dst_ports.inputs if p.name == dst.port), None)
        if out is None:
            found.append(Problem("unknown_port", f"{src.step} has no output {src.port!r}", step=src.step))
            continue
        if into is None:
            found.append(Problem("unknown_port", f"{dst.step} has no input {dst.port!r}", step=dst.step))
            continue
        reason = port_mismatch(out, into)
        if reason is not None:
            found.append(
                Problem("type_mismatch", f"{src.step}.{src.port}: {reason}", step=dst.step, port=dst.port)
            )
        if not into.control:
            fed[(dst.step, dst.port)] = fed.get((dst.step, dst.port), 0) + 1

    for (step_id, port), count in fed.items():
        if count > 1:
            found.append(Problem("duplicate_input", f"{count} edges feed one input", step=step_id, port=port))

    for step in spec.steps:
        resolved = ports[step.id]
        if resolved is None:
            continue
        for port in resolved.inputs:
            if port.required and (step.id, port.name) not in fed:
                found.append(
                    Problem("unconnected_input", "required input is not wired", step=step.id, port=port.name)
                )

    cyclic = _cyclic(graph)
    if cyclic:
        found.append(Problem("cycle", "these steps wait on each other: " + ", ".join(cyclic)))
    return found


def _cyclic(graph: dict[str, set[str]]) -> list[str]:
    """Steps on a cycle, sorted; empty for a DAG.

    Strongly connected components (Tarjan's algorithm), kept when they have more than one step or
    a step wired to itself. Kahn's algorithm is not enough: what it cannot order includes every
    step downstream of a cycle, and a step between two cycles survives running it both ways.
    Iterative, because the graph is an author's and recursion depth is not theirs to choose.
    """
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    found: list[str] = []
    for root in graph:
        if root in index:
            continue
        work: list[tuple[str, list[str]]] = [(root, sorted(graph[root]))]
        index[root] = low[root] = len(index)
        stack.append(root)
        on_stack.add(root)
        while work:
            node, pending = work[-1]
            if pending:
                nxt = pending.pop()
                if nxt not in index:
                    index[nxt] = low[nxt] = len(index)
                    stack.append(nxt)
                    on_stack.add(nxt)
                    work.append((nxt, sorted(graph[nxt])))
                elif nxt in on_stack:
                    low[node] = min(low[node], index[nxt])
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                component: list[str] = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                if len(component) > 1 or node in graph[node]:
                    found.extend(component)
    return sorted(found)


__all__ = [
    "MAX_EDGES",
    "MAX_STEPS",
    "PORT_NAME_PATTERN",
    "STEP_ID_PATTERN",
    "TRIGGER",
    "WHEN",
    "WORKFLOW_ID_PATTERN",
    "Edge",
    "Port",
    "PortRef",
    "Position",
    "Problem",
    "Step",
    "StepPorts",
    "WorkflowSpec",
    "describe",
    "port_mismatch",
    "problems",
    "resolve_ports",
]
