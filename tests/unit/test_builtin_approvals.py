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
    POST_ATTENDED_APPROVAL_RULE_ID,
    UNWAIVABLE_RULE_IDS,
    UNWAIVABLE_TOOLS,
    BuiltinApproval,
)
from felix.manifests.loader import parse_manifest
from felix.manifests.schema import ApprovalRule, Spec

from tests.support.factories import make_settings

IDS = [b.spec_field for b in BUILTIN_APPROVALS]
# What a block needs besides its `approval` to compile, for a block with a required field: the
# destinations it names must be registered for the tenant (`_SETTINGS`).
_BLOCK_FIELDS: dict[str, dict[str, Any]] = {"destinations": {"ids": ["team-brief"]}}
_SETTINGS = {
    "webhook_endpoints": '{"ops": {"url": "https://hooks.example.invalid/h", "secret": "s", "tenants": "*"}}',
    "destinations": '{"team-brief": {"kind": "webhook", "endpoint": "ops", "tenants": "*"}}',
}


def _block(builtin: BuiltinApproval, approval: bool) -> dict[str, Any]:
    return {builtin.spec_field: {**_BLOCK_FIELDS.get(builtin.spec_field, {}), "approval": approval}}


def _block_model(field: Any) -> Any:
    """A spec field's block model: the non-None member of `X | None`, or the annotation itself."""
    block = field.annotation
    return next(a for a in getattr(block, "__args__", (block,)) if a is not type(None))


def test_every_spec_block_with_an_approval_switch_has_a_row() -> None:
    """The converse of the row checks below: a block whose `approval` is not in the table would be a
    gate that parses, reads as "held for approval", and adds no rule -- and a mode would waive it."""
    switched = {
        name
        for name, field in Spec.model_fields.items()
        if hasattr(_block_model(field), "model_fields") and "approval" in _block_model(field).model_fields
    }
    assert switched == {b.spec_field for b in BUILTIN_APPROVALS}


def test_the_rows_are_the_ones_approval_rows_already_name() -> None:
    """A rule id is stored on every approval row it asked for, and a description is what an approver
    reads: neither changes by accident."""
    assert {(b.spec_field, b.tool, b.rule_id, b.description) for b in BUILTIN_APPROVALS} == {
        ("scheduling", "schedule_task", "scheduling.schedule_task", "Approve scheduling this task"),
        ("notify", "notify", "notify.notify", "Approve sending this notice"),
        ("destinations", "post", "destinations.post", "Approve this post"),
        (
            "workflow_authoring",
            "save_workflow",
            "workflow_authoring.save_workflow",
            "Approve saving this workflow",
        ),
    }


def test_the_unwaivable_sets_are_the_table_and_the_plan() -> None:
    # `post` is a name a plugin's tool may carry, so it is unwaivable by rule and by the block that
    # bound it (`bound_unwaivable_tools`), not by name in every manifest.
    assert {b.tool for b in BUILTIN_APPROVALS if b.unwaivable_tool} == UNWAIVABLE_TOOLS
    assert "post" not in UNWAIVABLE_TOOLS
    # Every rule id a row names -- its `approval: true` rule and the block's others -- and the plan's.
    assert {PLAN_APPROVAL_RULE_ID} | {
        rid for b in BUILTIN_APPROVALS for rid in (b.rule_id, *b.extra_rule_ids)
    } == UNWAIVABLE_RULE_IDS
    assert (
        POST_ATTENDED_APPROVAL_RULE_ID
        in next(b for b in BUILTIN_APPROVALS if b.tool == "post").extra_rule_ids
    )
    assert len({b.tool for b in BUILTIN_APPROVALS}) == len(BUILTIN_APPROVALS), "one row per tool"


@pytest.mark.parametrize("builtin", BUILTIN_APPROVALS, ids=IDS)
def test_each_row_names_a_spec_block_with_an_approval_switch(builtin: BuiltinApproval) -> None:
    field = Spec.model_fields.get(builtin.spec_field)
    assert field is not None, f"spec.{builtin.spec_field} does not exist"
    assert "approval" in _block_model(field).model_fields, f"spec.{builtin.spec_field} has no `approval`"


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
    deps = builder.BuildDeps(tools=default_tool_provider(), settings=make_settings(**_SETTINGS))
    agent = await builder.build_agent(manifest, deps=deps)
    return {t.name for t in agent.tools}, seen[0] if seen else []


@pytest.mark.parametrize("builtin", BUILTIN_APPROVALS, ids=IDS)
async def test_each_rows_block_binds_its_tool_and_compiles_its_rule(
    builtin: BuiltinApproval, monkeypatch: pytest.MonkeyPatch
) -> None:
    names, rules = await _compiled(_block(builtin, True), monkeypatch)
    assert builtin.tool in names, f"spec.{builtin.spec_field} does not bind {builtin.tool}"
    ids = [r.id for r in rules]
    assert ids.count(builtin.rule_id) == 1, f"spec.{builtin.spec_field} compiled {ids}, not {builtin.rule_id}"
    rule = next(r for r in rules if r.id == builtin.rule_id)
    assert (rule.tools, rule.one_shot, rule.bind_principal, rule.when_args) == (
        [builtin.tool],
        True,
        True,
        [],
    )

    _names, off = await _compiled(_block(builtin, False), monkeypatch)
    assert builtin.rule_id not in {r.id for r in off}


@pytest.mark.parametrize("builtin", BUILTIN_APPROVALS, ids=IDS)
async def test_the_manifests_own_literal_rule_displaces_the_builtin_and_a_glob_does_not(
    builtin: BuiltinApproval, monkeypatch: pytest.MonkeyPatch
) -> None:
    own = {"id": "mine", "tools": [builtin.tool]}
    _n, rules = await _compiled({**_block(builtin, True), "approvals": [own]}, monkeypatch)
    assert builtin.rule_id not in {r.id for r in rules}
    glob = {"id": "all", "tools": ["*"]}
    _n, rules = await _compiled({**_block(builtin, True), "approvals": [glob]}, monkeypatch)
    assert builtin.rule_id in {r.id for r in rules}
