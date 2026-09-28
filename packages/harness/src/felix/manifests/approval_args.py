"""Check that an approval rule's `when_args` names arguments its tools actually take.

`when_args` gates only the calls that carry those arguments, so a misspelled name is a rule that
never fires: `when_args: [topickey]` on `remember` leaves the retirement route it was written to
gate wide open, and nothing said so — `validate-manifest` and both framework checks passed it.

Two places check, because two kinds of tool exist:

* **Refused at write** (`validate_for_write`, so `PUT /manifests` and `felix validate-manifest`)
  for the tools whose schemas are known offline — the built-ins, plugin tools, and the memory
  tools. Their schemas ship with the harness, so a name missing from them is a mistake today and
  will still be one tomorrow.
* **Warned at compile** for every tool the manifest resolved, MCP included. An MCP server's schema
  is only known once it is listed, and it can change underneath a stored manifest; refusing there
  would turn an upstream rename into an outage for the manifest, so it is a warning and a counter.

A name is flagged only when it is an argument of *none* of the tools the rule reaches: a glob rule
such as `github__*` with `when_args: [force]` is written for the tools that take `force`, and the
others are correctly never gated by it. A tool whose schema lists no properties takes arguments
nobody can check, so it is never grounds for a flag.
"""

from __future__ import annotations

import logging
from typing import Any

from felix.manifests.tool_match import matches_any

logger = logging.getLogger("felix.manifests.approval_args")

# One warning per (manifest, rule tools, names) per process: the check runs on every compile,
# which is every request.
_warned: set[tuple[str, tuple[str, ...], tuple[str, ...]]] = set()


def tool_arg_names(tool: Any) -> frozenset[str] | None:
    """The arguments a tool declares, or None when its schema does not list them."""
    schema = getattr(tool, "raw_input_schema", None) or getattr(tool, "args_schema", None)
    if isinstance(schema, dict):
        props = schema.get("properties")
        return frozenset(props) if isinstance(props, dict) and props else None
    fields = getattr(schema, "model_fields", None)
    if isinstance(fields, dict) and fields:
        names = set(fields)
        names.update(f.alias for f in fields.values() if getattr(f, "alias", None))
        return frozenset(names)
    return None


def unknown_when_args(rule: Any, tools: list[Any]) -> tuple[list[str], list[str]]:
    """`(names no reached tool takes, the reached tools' names)` for one approval rule.

    Empty when the rule has no `when_args`, reaches none of `tools`, or reaches a tool whose
    arguments are not listed.
    """
    names = list(getattr(rule, "when_args", None) or [])
    if not names:
        return [], []
    patterns = list(getattr(rule, "tools", None) or [])
    reached = [t for t in tools if t.name in patterns or matches_any(patterns, t.name)]
    if not reached:
        return [], []
    known: set[str] = set()
    for tool in reached:
        args = tool_arg_names(tool)
        if args is None:
            return [], []
        known |= args
    return [n for n in names if n not in known], sorted(t.name for t in reached)


def _describe(rule: Any, unknown: list[str], reached: list[str], tools: list[Any]) -> str:
    takes = sorted({a for t in tools if t.name in reached for a in (tool_arg_names(t) or ())})
    return (
        f"approval rule for {list(rule.tools)}: when_args {unknown} names no argument of "
        f"{', '.join(reached)} (it takes: {', '.join(takes) or 'none'}), so the rule never fires"
    )


def offline_tools(settings: Any) -> list[Any]:
    """Every tool whose schema is known without a network: built-ins, plugins, memory tools."""
    from felix.memory.procedural import make_remember_procedure_tool
    from felix.memory.tools import make_memory_tools
    from felix.tools.builtins import default_tool_provider

    provider = default_tool_provider()
    tools = list(provider.resolve(provider.list_names()))
    # Bound to nothing: only their schemas are read, and constructing them does no I/O.
    tools += make_memory_tools(settings=settings, tenant_id="", manifest_id="")
    tools.append(make_remember_procedure_tool(settings=settings, tenant_id="", manifest_id=""))
    return tools


def when_args_problems(rules: list[Any], tools: list[Any]) -> list[str]:
    """The write-time half: problems in rules that name offline tools literally, and only those.

    A glob, or a name that is not an offline tool, may reach tools whose schemas are unknown
    here — `github__*` with `when_args: [force]` reaches no built-in that takes `force`, and is
    right. Those are left to the compile-time warning, which sees every resolved tool.
    """
    offline = {t.name for t in tools}
    problems = []
    for rule in rules:
        patterns = list(getattr(rule, "tools", None) or [])
        if not patterns or any(p not in offline for p in patterns):
            continue
        unknown, reached = unknown_when_args(rule, tools)
        if unknown:
            problems.append(_describe(rule, unknown, reached, tools))
    return problems


def warn_unknown_when_args(rules: list[Any], tools: list[Any], manifest_id: str) -> None:
    """The compile-time half: warn, count, never refuse."""
    for rule in rules:
        unknown, reached = unknown_when_args(rule, tools)
        if not unknown:
            continue
        key = (manifest_id, tuple(rule.tools), tuple(unknown))
        if key in _warned:
            continue
        _warned.add(key)
        from felix.observability.metrics import record_counter

        record_counter("felix_approval_when_args_unknown", {"manifest_id": manifest_id})
        logger.warning("manifest %s: %s", manifest_id, _describe(rule, unknown, reached, tools))


__all__ = [
    "offline_tools",
    "tool_arg_names",
    "unknown_when_args",
    "warn_unknown_when_args",
    "when_args_problems",
]
