"""`permission_mode.BUILTIN_APPROVALS`: each row is a real, compiled, unwaivable gate.

The table is where an approval a spec block adds over its tool is declared once, and the sets the
permission modes consult are derived from it. What it cannot say by itself is that a row is wired:
that its `spec_field` is a block with an `approval` switch, that the block binds `tool`, and that
compiling the manifest adds `rule_id` over it. A row that names a field nothing reads would be a gate
that looks present and does nothing, so each one is compiled here.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.governance.permission_mode import (
    BUILTIN_APPROVALS,
    PLAN_APPROVAL_RULE_ID,
    UNWAIVABLE_RULE_IDS,
    UNWAIVABLE_TOOLS,
    BuiltinApproval,
)
from felix.manifests.loader import parse_manifest
from felix.manifests.schema import ApprovalRule, Spec

from tests.support.factories import make_settings

IDS = [b.spec_field for b in BUILTIN_APPROVALS]


def test_the_unwaivable_sets_are_the_table_and_the_plan() -> None:
    assert {b.tool for b in BUILTIN_APPROVALS} == UNWAIVABLE_TOOLS
    assert {PLAN_APPROVAL_RULE_ID} | {b.rule_id for b in BUILTIN_APPROVALS} == UNWAIVABLE_RULE_IDS
    assert len({b.tool for b in BUILTIN_APPROVALS}) == len(BUILTIN_APPROVALS), "one row per tool"


@pytest.mark.parametrize("builtin", BUILTIN_APPROVALS, ids=IDS)
def test_each_row_names_a_spec_block_with_an_approval_switch(builtin: BuiltinApproval) -> None:
    field = Spec.model_fields.get(builtin.spec_field)
    assert field is not None, f"spec.{builtin.spec_field} does not exist"
    block = field.annotation
    # `X | None`: the block's own model is the non-None member.
    model = next(a for a in getattr(block, "__args__", (block,)) if a is not type(None))
    assert "approval" in model.model_fields, f"spec.{builtin.spec_field} has no `approval`"


async def _compiled(
    spec: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> tuple[set[str], list[ApprovalRule]]:
    """The bound tool names and the approval rules `build_agent` hands to `apply_approvals`."""
    from felix.manifests import builder
    from felix.tools.builtins import default_tool_provider

    seen: list[list[ApprovalRule]] = []
    real = builder.apply_approvals

    def _spy(tools: Any, rules: list[ApprovalRule], *a: Any, **k: Any) -> Any:
        seen.append(list(rules))
        return real(tools, rules, *a, **k)

    monkeypatch.setattr(builder, "apply_approvals", _spy)
    manifest = parse_manifest(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": "gated"},
            "spec": {"pattern": "react", **spec},
        }
    )
    deps = builder.BuildDeps(tools=default_tool_provider(), settings=make_settings())
    agent = await builder.build_agent(manifest, deps=deps)
    return {t.name for t in agent.tools}, seen[0] if seen else []


@pytest.mark.parametrize("builtin", BUILTIN_APPROVALS, ids=IDS)
async def test_each_rows_block_binds_its_tool_and_compiles_its_rule(
    builtin: BuiltinApproval, monkeypatch: pytest.MonkeyPatch
) -> None:
    names, rules = await _compiled({builtin.spec_field: {"approval": True}}, monkeypatch)
    assert builtin.tool in names, f"spec.{builtin.spec_field} does not bind {builtin.tool}"
    [rule] = [r for r in rules if r.id == builtin.rule_id]
    assert (rule.tools, rule.one_shot, rule.bind_principal, rule.when_args) == (
        [builtin.tool],
        True,
        True,
        [],
    )

    _names, off = await _compiled({builtin.spec_field: {"approval": False}}, monkeypatch)
    assert builtin.rule_id not in {r.id for r in off}


@pytest.mark.parametrize("builtin", BUILTIN_APPROVALS, ids=IDS)
async def test_the_manifests_own_literal_rule_displaces_the_builtin_and_a_glob_does_not(
    builtin: BuiltinApproval, monkeypatch: pytest.MonkeyPatch
) -> None:
    own = {"id": "mine", "tools": [builtin.tool]}
    _n, rules = await _compiled({builtin.spec_field: {"approval": True}, "approvals": [own]}, monkeypatch)
    assert builtin.rule_id not in {r.id for r in rules}
    glob = {"id": "all", "tools": ["*"]}
    _n, rules = await _compiled({builtin.spec_field: {"approval": True}, "approvals": [glob]}, monkeypatch)
    assert builtin.rule_id in {r.id for r in rules}
