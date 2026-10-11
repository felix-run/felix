"""A manifest that gates writes and binds a delete or a rename ungated is warned about.

Warned, never refused: at `felix validate-manifest` (exit 0, a `warning` line), in
`PUT /manifests/{name}`'s `warnings` list, and in the log once per process at compile.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from felix.manifests import delete_gate
from felix.manifests.delete_gate import delete_gate_gaps, delete_gate_warnings
from felix.manifests.governance import manifest_warnings, validate_for_write
from felix.manifests.loader import parse_manifest
from felix.manifests.schema import Manifest

from tests.support.factories import app_client, make_settings

_PATH_ONLY = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}


def _client_tool(name: str) -> dict[str, Any]:
    return {"name": name, "description": name, "args_schema": _PATH_ONLY}


def _manifest(
    *,
    tools: list[str] | None = None,
    client_tools: list[str] | None = None,
    approvals: list[dict[str, Any]] | None = None,
    name: str = "operator",
) -> dict[str, Any]:
    return {
        "apiVersion": "felix/v1",
        "kind": "Agent",
        "metadata": {"name": name},
        "spec": {
            "tools": tools or [],
            "client_tools": [_client_tool(n) for n in client_tools or []],
            "approvals": approvals or [],
        },
    }


def _parse(**kw: Any) -> Manifest:
    return parse_manifest(_manifest(**kw))


def _rule(rule_id: str, *tools: str) -> dict[str, Any]:
    return {"id": rule_id, "tools": list(tools), "ttl_seconds": 60}


SERVER_GAP = _manifest(
    tools=["write_file", "edit_file", "delete_file", "rename_file"],
    approvals=[_rule("workspace-write", "write_file", "edit_file", "rename_file")],
)


def test_the_server_family_names_the_ungated_tool_and_the_rule_that_gates_the_write() -> None:
    gaps = delete_gate_gaps(parse_manifest(SERVER_GAP))
    assert [(g.family, g.writer, g.rule, g.ungated) for g in gaps] == [
        ("server", "write_file", "workspace-write", ("delete_file",))
    ]
    (message,) = delete_gate_warnings(parse_manifest(SERVER_GAP))
    assert message == (
        "approval rule `workspace-write` gates write_file, but delete_file is bound with no approval "
        "rule, so the agent can delete or move files without asking when it must ask to write them; "
        "add delete_file to rule `workspace-write`"
    )


def test_the_client_family_names_every_ungated_tool() -> None:
    m = _parse(
        client_tools=["local_write", "local_edit", "local_delete", "local_rename"],
        approvals=[_rule("folder-writes", "local_edit")],
    )
    (gap,) = delete_gate_gaps(m)
    assert (gap.family, gap.writer, gap.rule, gap.ungated) == (
        "client",
        "local_edit",
        "folder-writes",
        ("local_delete", "local_rename"),
    )
    assert "add local_delete, local_rename to rule `folder-writes`" in gap.message


def test_both_families_are_reported_independently() -> None:
    m = _parse(
        tools=["write_file", "delete_file"],
        client_tools=["local_write", "local_rename"],
        approvals=[_rule("w", "write_file", "local_write")],
    )
    assert [(g.family, g.ungated) for g in delete_gate_gaps(m)] == [
        ("server", ("delete_file",)),
        ("client", ("local_rename",)),
    ]


@pytest.mark.parametrize(
    "spec",
    [
        # Gated alike, by one rule.
        {
            "tools": ["write_file", "delete_file", "rename_file"],
            "approvals": [_rule("w", "write_file", "delete_file", "rename_file")],
        },
        # Gated alike, by a glob and by separate rules.
        {"tools": ["write_file", "delete_file"], "approvals": [_rule("all", "*")]},
        {
            "client_tools": ["local_write", "local_delete"],
            "approvals": [_rule("w", "local_write"), _rule("d", "local_delete")],
        },
        # No writer is gated: nothing to be less than.
        {"tools": ["write_file", "delete_file"], "approvals": []},
        {"tools": ["write_file", "delete_file"], "approvals": [_rule("shell", "local_shell")]},
        # No deleter or renamer bound.
        {"tools": ["write_file", "edit_file"], "approvals": [_rule("w", "write_file")]},
        # A writer gated but not bound: the rule gates nothing (warned about elsewhere).
        {"tools": ["delete_file"], "approvals": [_rule("w", "write_file")]},
        # The converse — a delete gated, a write not — is a choice, not a hole.
        {"tools": ["write_file", "delete_file"], "approvals": [_rule("d", "delete_file")]},
        # Families do not cross: a gated server write says nothing about a client delete.
        {
            "tools": ["write_file"],
            "client_tools": ["local_delete"],
            "approvals": [_rule("w", "write_file")],
        },
    ],
    ids=[
        "one-rule",
        "glob",
        "separate-rules",
        "no-rules",
        "writer-not-gated",
        "no-deleter",
        "writer-unbound",
        "converse",
        "families-separate",
    ],
)
def test_no_warning(spec: dict[str, Any]) -> None:
    m = _parse(**spec)
    assert delete_gate_gaps(m) == []
    assert manifest_warnings(m) == []


def test_it_is_a_warning_and_never_a_refusal() -> None:
    m = parse_manifest(SERVER_GAP)
    validate_for_write(m)  # does not raise
    assert manifest_warnings(m) == delete_gate_warnings(m)


def _write(tmp_path: Path, manifest: dict[str, Any]) -> Path:
    import yaml

    path = tmp_path / "operator.yaml"
    path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
    return path


def test_validate_manifest_prints_it_and_still_passes(tmp_path: Path) -> None:
    from felix_cli.main import app
    from typer.testing import CliRunner

    result = CliRunner().invoke(
        app, ["validate-manifest", str(_write(tmp_path, SERVER_GAP)), "--no-resolve-egress"]
    )
    assert result.exit_code == 0, result.output
    out = " ".join(result.output.split())  # rich wraps long lines
    assert "warning" in out and "delete_file" in out and "`workspace-write`" in out, out
    assert "ok" in out


def test_validate_manifest_is_quiet_when_gated_alike(tmp_path: Path) -> None:
    from felix_cli.main import app
    from typer.testing import CliRunner

    gated = _manifest(
        tools=["write_file", "delete_file"], approvals=[_rule("w", "write_file", "delete_file")]
    )
    result = CliRunner().invoke(
        app, ["validate-manifest", str(_write(tmp_path, gated)), "--no-resolve-egress"]
    )
    assert result.exit_code == 0, result.output
    assert "warning" not in result.output


async def test_put_manifest_stores_it_and_returns_the_warning() -> None:
    async with app_client(make_settings()) as client:
        resp = await client.put("/manifests/operator", json={"manifest": SERVER_GAP})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["warnings"] == delete_gate_warnings(parse_manifest(SERVER_GAP))
        assert body["version"] >= 1

        gated = _manifest(tools=["write_file", "delete_file"], approvals=[_rule("w", "write_file", "*")])
        resp = await client.put("/manifests/operator", json={"manifest": gated})
        assert resp.status_code == 200, resp.text
        # Always present, empty when there is nothing to say.
        assert resp.json()["warnings"] == []


async def test_compile_logs_it_once_per_manifest_content(caplog: pytest.LogCaptureFixture) -> None:
    from felix.manifests.builder import build_agent
    from felix.tools.builtins import default_tool_provider

    delete_gate._warned.clear()
    settings = make_settings()
    with caplog.at_level(logging.WARNING, logger="felix.manifests.delete_gate"):
        await build_agent(SERVER_GAP, default_tool_provider(), settings=settings)
        await build_agent(SERVER_GAP, default_tool_provider(), settings=settings)
    lines = [r.getMessage() for r in caplog.records if r.name == "felix.manifests.delete_gate"]
    assert len(lines) == 1, lines
    assert "'operator'" in lines[0] and "delete_file" in lines[0] and "workspace-write" in lines[0]

    # A version that changes the gap is said again.
    caplog.clear()
    changed = _manifest(
        tools=["write_file", "delete_file", "rename_file"], approvals=[_rule("workspace-write", "write_file")]
    )
    with caplog.at_level(logging.WARNING, logger="felix.manifests.delete_gate"):
        await build_agent(changed, default_tool_provider(), settings=settings)
    lines = [r.getMessage() for r in caplog.records if r.name == "felix.manifests.delete_gate"]
    assert len(lines) == 1 and "delete_file, rename_file" in lines[0], lines


async def test_compile_is_quiet_when_gated_alike(caplog: pytest.LogCaptureFixture) -> None:
    from felix.manifests.builder import build_agent
    from felix.tools.builtins import default_tool_provider

    delete_gate._warned.clear()
    gated = _manifest(
        tools=["write_file", "delete_file"], approvals=[_rule("w", "write_file", "delete_file")]
    )
    with caplog.at_level(logging.WARNING, logger="felix.manifests.delete_gate"):
        await build_agent(gated, default_tool_provider(), settings=make_settings())
    assert not [r for r in caplog.records if r.name == "felix.manifests.delete_gate"]
