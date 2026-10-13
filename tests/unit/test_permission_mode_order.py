"""The order permission modes loosen in, and the plan-mode names a manifest may not borrow.

`POST /chat/mode` asks `widens` whether a move needs `approvals:write`; a mode added to
`PermissionModeName` without a place in that order would read as `plan` and make every move to it
a widening -- or, worse, be forgotten in a way that let one through. These pin the table to the
type, and the edge readings the route relies on.
"""

from __future__ import annotations

from typing import Any, get_args

import pytest
from felix.governance import permission_mode
from felix.governance.permission_mode import _LATITUDE, read_thread_mode, widens
from felix.manifests.loader import ManifestParseError, parse_manifest
from felix.manifests.schema import RESERVED_PLAN_RULE_ID, RESERVED_PLAN_TOOL_NAME, PermissionModeName

from tests.support.factories import make_settings


def test_every_mode_has_a_place_in_the_order() -> None:
    assert set(_LATITUDE) == set(get_args(PermissionModeName))


@pytest.mark.parametrize(
    ("current", "new", "expected"),
    [
        (None, "accept_edits", True),  # never set is `default`
        (None, "default", False),
        (None, "plan", False),
        ("plan", "default", True),
        ("accept_edits", "default", False),
        ("default", "bypass", True),
        ("no-such-mode", "default", True),  # an unknown stored mode reads as `plan`
        ("default", "no-such-mode", False),  # a run honours nothing it does not know
    ],
)
def test_widens(current: str | None, new: str, expected: bool) -> None:
    assert widens(current, new) is expected


async def test_an_unreadable_mode_reads_as_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken(**_: Any) -> dict[str, Any]:
        raise RuntimeError("store down")

    monkeypatch.setattr(permission_mode, "get_thread_meta", broken)
    assert await read_thread_mode(make_settings(), "t", "t:x") == "plan"


async def test_a_thread_with_no_mode_reads_as_none_and_one_with_a_mode_as_it() -> None:
    from felix.session.thread_state import update_thread_meta

    settings = make_settings()
    assert await read_thread_mode(settings, "t", "t:x") is None
    assert await read_thread_mode(settings, "t", None) is None
    await update_thread_meta(
        settings=settings, tenant_id="t", thread_id="t:x", permission_mode="accept_edits"
    )
    assert await read_thread_mode(settings, "t", "t:x") == "accept_edits"


def test_the_reserved_names_are_the_ones_plan_mode_uses() -> None:
    assert RESERVED_PLAN_TOOL_NAME == permission_mode.EXIT_PLAN_TOOL_NAME
    assert RESERVED_PLAN_RULE_ID == permission_mode.PLAN_APPROVAL_RULE_ID
    assert permission_mode.plan_approval_rule().id == RESERVED_PLAN_RULE_ID


def _spec(**spec: Any) -> dict[str, Any]:
    return {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": "m"}, "spec": spec}


@pytest.mark.parametrize(
    "spec",
    [
        {"approvals": [{"id": "plan-approval", "tools": ["calculator"]}]},
        {"approvals": [{"id": "mine", "tools": ["exit_plan_mode"]}]},
        {"tools": ["exit_plan_mode"]},
        {"client_tools": [{"name": "exit_plan_mode"}]},
    ],
    ids=["rule-id", "rule-tools", "tools", "client-tool"],
)
def test_a_manifest_cannot_declare_plan_mode_s_tool_or_rule(spec: dict[str, Any]) -> None:
    """Whichever mode the manifest allows: `approvals.may_decide` exempts that rule on that tool,
    so a manifest naming either would borrow the exemption."""
    with pytest.raises(ManifestParseError, match=r"reserved|plan mode"):
        parse_manifest(_spec(**spec))


def test_a_manifest_offering_plan_mode_still_compiles_its_plan_rule() -> None:
    manifest = parse_manifest(_spec(permissions={"allowed_modes": ["default", "plan"]}))
    assert "plan" in manifest.spec.permissions.allowed_modes
