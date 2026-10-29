"""`when_args` must name an argument some gated tool takes, or the rule never fires.

Refused at write for tools whose schemas ship with the harness; warned at compile for the rest,
because an MCP tool's schema can change under a stored manifest and that must not be an outage.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from felix.manifests.governance import GovernanceError, validate_for_write
from felix.manifests.loader import parse_manifest
from felix.manifests.schema import ApprovalRule
from felix.tools.types import Tool

#: Tools the test environment itself cannot build: the stock tools, with `FELIX_MARKET_API_KEY` blank.
_UNBUILDABLE_HERE = {"connectors__quote", "connectors__quote_history", "connectors__symbol_search"}


def _manifest(*rules: dict[str, Any]) -> Any:
    return parse_manifest(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": "gated"},
            "spec": {"memory": {"recall": {"tools": True}}, "approvals": list(rules)},
        }
    )


def test_a_misspelled_name_on_a_known_tool_is_refused_at_write() -> None:
    with pytest.raises(GovernanceError) as exc:
        validate_for_write(_manifest({"id": "r1", "tools": ["remember"], "when_args": ["topickey"]}))
    message = str(exc.value)
    assert "topickey" in message and "remember" in message and "topic_key" in message, message


def test_a_name_the_tool_takes_is_accepted() -> None:
    validate_for_write(_manifest({"id": "r1", "tools": ["remember"], "when_args": ["topic_key"]}))


def test_a_glob_is_left_to_compile_time() -> None:
    """`re*` with `when_args: [force]` reaches no built-in that takes `force` — `read_file`,
    `remember`, `recall` — and is right to, since it was written for tools only an MCP server
    knows about. (Not `*`: that also reaches a tool with an unlisted schema, which excuses the
    rule on its own and would let this pass for the wrong reason.)"""
    validate_for_write(_manifest({"id": "r1", "tools": ["re*"], "when_args": ["force"]}))


def test_a_tool_that_cannot_be_built_offline_does_not_refuse_every_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A plugin tool missing its key raises when built. The write of a manifest that does not name
    # it must still pass, and one naming it is left to the compile, which refuses the tool itself.
    from felix.tools import builtins

    real = builtins.default_tool_provider

    def provider() -> Any:
        p = real()

        def unbuildable() -> Tool:
            raise ValueError("FELIX_EXAMPLE_KEY is not set")

        p.register("plugin__keyed", unbuildable)
        return p

    monkeypatch.setattr(builtins, "default_tool_provider", provider)
    validate_for_write(_manifest({"id": "r1", "tools": ["remember"], "when_args": ["topic_key"]}))
    validate_for_write(_manifest({"id": "r2", "tools": ["plugin__keyed"], "when_args": ["anything"]}))
    # Skipping one tool is not checking none: a provider built-in and a memory tool still refuse.
    for tool, typo in (("read_file", "paht"), ("remember", "topickey")):
        with pytest.raises(GovernanceError):
            validate_for_write(_manifest({"id": "r3", "tools": [tool], "when_args": [typo]}))
    from felix.config import get_settings
    from felix.manifests.approval_args import offline_tools

    names = {t.name for t in offline_tools(get_settings())}
    assert "plugin__keyed" not in names
    assert set(real().list()) <= names | {"plugin__keyed"} | _UNBUILDABLE_HERE


def test_a_factory_that_fails_for_another_reason_still_fails_the_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Only a configuration refusal (ValueError) is skipped; a broken factory is a defect to hear about.
    from felix.tools import builtins

    real = builtins.default_tool_provider

    def provider() -> Any:
        p = real()

        def broken() -> Tool:
            raise TypeError("a bug in a factory")

        p.register("plugin__broken", broken)
        return p

    monkeypatch.setattr(builtins, "default_tool_provider", provider)
    with pytest.raises(TypeError):
        validate_for_write(_manifest({"id": "r1", "tools": ["remember"], "when_args": ["topic_key"]}))


def test_the_cli_fails_on_it(tmp_path: Path) -> None:
    from felix_cli.main import app
    from typer.testing import CliRunner

    path = tmp_path / "gated.yaml"
    path.write_text(
        "apiVersion: felix/v1\nkind: Agent\nmetadata: {name: gated}\n"
        "spec:\n  approvals:\n    - {id: r1, tools: [remember], when_args: [topickey]}\n",
        encoding="utf-8",
    )
    result = CliRunner().invoke(app, ["validate-manifest", str(path), "--no-resolve-egress"])
    assert result.exit_code != 0, result.output
    assert "topickey" in result.output


def _tool(name: str, *props: str) -> Tool:
    schema = {"type": "object", "properties": {p: {"type": "string"} for p in props}}
    return Tool(name=name, description="", args_schema=schema, executor=None, raw_input_schema=schema)


def test_compile_warns_once_about_a_name_no_resolved_tool_takes(caplog: pytest.LogCaptureFixture) -> None:
    from felix.manifests import approval_args
    from felix.manifests.builder import apply_approvals

    approval_args._warned.clear()
    tools = [_tool("gh__push", "repo", "branch")]
    rule = ApprovalRule(id="r1", tools=["gh__push"], when_args=["force"])
    with caplog.at_level(logging.WARNING, logger="felix.manifests.approval_args"):
        apply_approvals(tools, [rule], "m")
        apply_approvals(tools, [rule], "m")
    warnings = [r.getMessage() for r in caplog.records if "never fires" in r.getMessage()]
    assert len(warnings) == 1, warnings
    assert "force" in warnings[0] and "gh__push" in warnings[0]


def test_compile_is_quiet_when_some_reached_tool_takes_it(caplog: pytest.LogCaptureFixture) -> None:
    from felix.manifests import approval_args
    from felix.manifests.builder import apply_approvals

    approval_args._warned.clear()
    tools = [_tool("gh__push", "repo", "force"), _tool("gh__list", "repo")]
    with caplog.at_level(logging.WARNING, logger="felix.manifests.approval_args"):
        apply_approvals(tools, [ApprovalRule(id="r1", tools=["gh__*"], when_args=["force"])], "m")
    assert not [r for r in caplog.records if "never fires" in r.getMessage()]


def test_a_tool_whose_schema_lists_no_arguments_is_never_grounds() -> None:
    """`activate_skill` declares no properties; nothing can say what it takes, so nothing is
    refused on its account."""
    validate_for_write(_manifest({"id": "r1", "tools": ["activate_skill"], "when_args": ["name"]}))
