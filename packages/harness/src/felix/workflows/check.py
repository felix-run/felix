"""Checking a workflow's graph: every step's ports, and everything that stops it running.

`check()` resolves each step's ports through its kind and walks the edges, returning the ports it
resolved beside the problems it found, so a caller that needs both (the editor's response, the run
engine) resolves each step once. It returns rather than raises: see `schema.py`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from pydantic import ValidationError

from felix.logging_setup import loggable
from felix.workflows.kinds import get_step_kind
from felix.workflows.ports import WHEN, Port, StepPorts, port_mismatch
from felix.workflows.schema import TRIGGER, Step, WorkflowSpec

logger = logging.getLogger("felix.workflows.check")

# Closed on purpose, unlike step kinds: this is the vocabulary a client switches on, and a code
# renamed or added without a reviewed diff would break the editor silently.
ProblemCode = Literal[
    "unknown_kind",
    "invalid_config",
    "kind_error",
    "unknown_step",
    "unknown_port",
    "type_mismatch",
    "duplicate_input",
    "unconnected_input",
    "cycle",
    # A stored definition a later schema no longer parses; the route reports it rather than
    # failing the read, so the editor can show the workflow and what is wrong with it.
    "invalid_definition",
    # The check stopped listing at `MAX_PROBLEMS`; this last entry says how many it left out.
    "too_many_problems",
]

# A response lists at most this many problems. An editor shows the first screenful anyway, and an
# unbounded list is memory a caller can multiply with every edge they add.
MAX_PROBLEMS = 200


@dataclass(frozen=True, slots=True)
class Problem:
    """One thing that stops a workflow running. `code` is stable; `message` is for a person."""

    code: ProblemCode
    message: str
    step: str | None = None
    port: str | None = None


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


def check(spec: WorkflowSpec) -> tuple[dict[str, StepPorts | None], list[Problem]]:
    """Each node's ports (`None` where they cannot be known) and everything that stops `spec`
    running, in a stable order. The map includes `trigger`; no problems means it can run."""
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
    if len(found) > MAX_PROBLEMS:
        left_out = len(found) - MAX_PROBLEMS
        found = [*found[:MAX_PROBLEMS], Problem("too_many_problems", f"{left_out} more problems not listed")]
    return ports, found


def problems(spec: WorkflowSpec) -> list[Problem]:
    """Everything that stops `spec` running; empty when it can run."""
    return check(spec)[1]


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


__all__ = ["MAX_PROBLEMS", "Problem", "ProblemCode", "check", "problems", "resolve_ports"]
