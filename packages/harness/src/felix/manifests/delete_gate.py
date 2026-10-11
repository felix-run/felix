"""A delete or a rename is never gated less than a write.

`delete_file` / `rename_file` (and a client's `local_delete` / `local_rename`) change the
workspace as surely as `write_file` / `edit_file` do, and a delete has no undo. A manifest that
puts writes behind an approval rule and binds a delete with no rule over it lets the agent remove
a file it would have had to ask to change — the opposite of what the rule was written for.

One definition, used everywhere the question is asked:

* `felix validate-manifest` prints each gap as a warning and still exits 0 (1 under `--strict`);
* `PUT /manifests/{name}` returns them in the response's `warnings` list;
* `build_agent` logs each once per process for a given manifest content (`warn_ungated_deletes`);
* the bundled-manifest test (`tests/unit/test_workspace_delete_rename_tools.py`) asserts none of
  ours has one.

A warning, never a refusal. A manifest stored before `delete_file` existed and since edited to
bind it is a working agent; refusing it at write, or at compile, would turn an upgrade into an
outage for a combination that is unwise rather than broken. The same trade `approval_args` makes
for an MCP tool's `when_args`.

"Gated" means *some* approval rule's `tools` matches the name (globs included) — the same test
`apply_approvals` uses to pick a rule. A rule with `when_args` gates only some calls; it still
counts, on both sides, because it is the operator saying which calls of that tool need a person.
Only the direction above is reported: gating a delete and not a write is a choice, not a hole.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from felix.manifests.tool_match import matches_any

logger = logging.getLogger("felix.manifests.delete_gate")

# (family, writers, the tools that must be gated like them). Server tools bind through
# `spec.tools`; client tools are declared in `spec.client_tools` and run in the caller's client.
FAMILIES: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("server", ("write_file", "edit_file"), ("delete_file", "rename_file")),
    ("client", ("local_write", "local_edit"), ("local_delete", "local_rename")),
)

# One log line per (tenant, manifest, gaps) per process: the check runs on every compile, which is
# every request. Keyed on the gaps themselves so a new version that changes them is said again.
_warned: set[tuple[str, str, tuple[str, ...]]] = set()


@dataclass(frozen=True)
class DeleteGateGap:
    """A family whose writer is gated while a bound delete or rename is not."""

    family: str
    # The gated writer and the first rule that gates it — the rule the fix should extend.
    writer: str
    rule: str
    ungated: tuple[str, ...]

    @property
    def message(self) -> str:
        tools = ", ".join(self.ungated)
        verb = "is" if len(self.ungated) == 1 else "are"
        return (
            f"approval rule `{self.rule}` gates {self.writer}, but {tools} {verb} bound with no "
            f"approval rule, so the agent can delete or move files without asking when it must ask "
            f"to write them; add {tools} to rule `{self.rule}`"
        )


def gating_rules(manifest: Any, tool: str) -> list[str]:
    """The ids of every approval rule whose `tools` reaches `tool`, in manifest order."""
    return [rule.id for rule in manifest.spec.approvals if matches_any(rule.tools, tool)]


def bound_tool_names(manifest: Any) -> set[str]:
    """The workspace-relevant names a manifest binds: `spec.tools` plus declared client tools."""
    spec = manifest.spec
    return {str(t) for t in spec.tools} | {t.name for t in spec.client_tools}


def delete_gate_gaps(manifest: Any) -> list[DeleteGateGap]:
    """Each family in which a bound writer is gated and a bound delete or rename is not."""
    bound = bound_tool_names(manifest)
    gaps: list[DeleteGateGap] = []
    for family, writers, changers in FAMILIES:
        gated = [(w, rules[0]) for w in writers if w in bound and (rules := gating_rules(manifest, w))]
        if not gated:
            continue
        ungated = tuple(c for c in changers if c in bound and not gating_rules(manifest, c))
        if ungated:
            writer, rule = gated[0]
            gaps.append(DeleteGateGap(family=family, writer=writer, rule=rule, ungated=ungated))
    return gaps


def delete_gate_warnings(manifest: Any) -> list[str]:
    """`delete_gate_gaps` as the sentences an author reads."""
    return [gap.message for gap in delete_gate_gaps(manifest)]


def warn_ungated_deletes(manifest: Any, tenant_id: str = "") -> None:
    """The compile-time half: log and count each gap, once per process for this content."""
    messages = tuple(delete_gate_warnings(manifest))
    if not messages:
        return
    name = manifest.metadata.name
    key = (tenant_id, name, messages)
    if key in _warned:
        return
    _warned.add(key)
    from felix.observability.metrics import record_counter

    record_counter("felix_delete_gated_less_than_write", {"manifest_id": name})
    for message in messages:
        logger.warning(
            "manifest %r: %s",
            name,
            message,
            extra={"manifest_id": name, "tenant_id": tenant_id},
        )


__all__ = [
    "FAMILIES",
    "DeleteGateGap",
    "bound_tool_names",
    "delete_gate_gaps",
    "delete_gate_warnings",
    "gating_rules",
    "warn_ungated_deletes",
]
