"""The authored workflow definition: typed steps wired port to port.

A workflow is a DAG. Each step is an instance of a *step kind* (`kinds.py`, an open registry),
and its ports (`ports.py`) come from the kind and the step's config. An edge joins one step's
output port to another step's input port: the data rides on the edge, and the edge is also the
ordering. A step with an edge into its control input `when` runs only if one of those edges fired.

The workflow's own inputs are the outputs of a node that is not a step, `trigger`: what a manual
run's form, a job's payload or an inbound event supplies.

This module validates *structure* on parse. The graph is checked by `check.py`, which returns
problems rather than raising, because the editor saves half-wired drafts: a workflow with problems
can be stored, and only a run refuses one.

A step's `kind` is an open `str` and its `config` a plain dict, validated by the kind in the check
rather than on parse. Both are deliberate. A stored workflow naming a kind whose plugin was later
uninstalled, or a config a kind later tightened, must still load so the editor can say what is
wrong with it; a stored row that stopped parsing is the outage `manifests/compat.py` documents for
manifests.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from felix.workflows.ports import PORT_NAME_PATTERN

# A workflow id is a URL path segment; a step id keeps to the port-name alphabet, since edges and
# templates reference both the same way.
WORKFLOW_ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"
STEP_ID_PATTERN = PORT_NAME_PATTERN
_PORT_NAME = re.compile(PORT_NAME_PATTERN)

# The node whose outputs are the workflow's inputs. Not a step, so no step may take its id.
TRIGGER = "trigger"

MAX_STEPS = 200
MAX_EDGES = 1000
# The whole definition, serialized. The HTTP body limit bounds a request, not a definition: a job,
# a plugin or the CLI can build one too, and every schema in it is walked by the check.
MAX_DEFINITION_BYTES = 256 * 1024


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
            if not _PORT_NAME.fullmatch(name):
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
        size = len(self.model_dump_json().encode())
        if size > MAX_DEFINITION_BYTES:
            raise ValueError(f"definition is {size} bytes; the limit is {MAX_DEFINITION_BYTES}")
        return self


__all__ = [
    "MAX_DEFINITION_BYTES",
    "MAX_EDGES",
    "MAX_STEPS",
    "STEP_ID_PATTERN",
    "TRIGGER",
    "WORKFLOW_ID_PATTERN",
    "Edge",
    "PortRef",
    "Position",
    "Step",
    "WorkflowSpec",
]
