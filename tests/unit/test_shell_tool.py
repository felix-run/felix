"""`spec.shell_tools` — what the governed shell must not be able to do.

Each test names one thing the module docstring of `felix/tools/shell.py` promises the tool
cannot do, and proves it against a real subprocess: there is no fake here, because the thing
under test is the boundary between the model's argv and the host.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from felix.config import Settings
from felix.context import AuthContext, RequestContext, async_run_with_context
from felix.manifests import builder
from felix.manifests.governance import GovernanceError, validate_for_write
from felix.manifests.loader import parse_manifest
from felix.manifests.schema import ShellToolRef
from felix.security.shell_policy import (
    ShellNotAllowedError,
    assert_shell_commands_allowed,
    prefix_covers,
)
from felix.tools.shell import MAX_OUTPUT_BYTES, tools_from_shell_refs
from felix.tools.types import Tool, ToolInvocationCtx, is_wrapper_deny, tool_output_content

PY = sys.executable


def _settings(ws: Path, allowed: str) -> Settings:
    return Settings(workspace_root=str(ws), shell_allowed_commands=allowed)


def _tool(
    ws: Path, *, commands: list[str], allowed: str | None = None, timeout_ms: int | None = None
) -> Tool:
    settings = _settings(ws, ", ".join(commands) if allowed is None else allowed)
    ref = ShellToolRef(name="run", commands=commands, timeout_ms=timeout_ms)
    return tools_from_shell_refs([ref], settings=settings)[0]


async def _run(tool: Tool, ws: Path, args: dict[str, Any], *, allowed: str = "") -> Any:
    settings = _settings(
        ws, allowed or ", ".join(tool.executor._prefixes and [" ".join(p) for p in tool.executor._prefixes])
    )  # type: ignore[attr-defined]
    ctx = RequestContext(settings=settings, auth=AuthContext(tenant_id="t"), manifest_id="m", thread_id="th")
    async with async_run_with_context(ctx):
        return await tool.executor.execute(args, ToolInvocationCtx())


def _result(out: Any) -> dict[str, Any]:
    text = tool_output_content(out)
    assert not text.startswith("shell_error"), text
    return json.loads(text)


# ---------------------------------------------------------------------------
# The allowlist grammar
# ---------------------------------------------------------------------------


def test_disabled_by_default(tmp_path: Path) -> None:
    ref = ShellToolRef(name="run", commands=["git status"])
    with pytest.raises(ShellNotAllowedError, match="disabled"):
        assert_shell_commands_allowed([ref], _settings(tmp_path, ""))


def test_a_manifest_prefix_must_be_covered_by_an_operator_prefix(tmp_path: Path) -> None:
    ref = ShellToolRef(name="run", commands=["git push"])
    with pytest.raises(ShellNotAllowedError, match="git push"):
        assert_shell_commands_allowed([ref], _settings(tmp_path, "git status, git diff"))
    # The operator's `git` covers the manifest's `git status`; a manifest narrows, never widens.
    assert_shell_commands_allowed(
        [ShellToolRef(name="run", commands=["git status"])], _settings(tmp_path, "git")
    )


def test_prefixes_match_token_for_token() -> None:
    assert prefix_covers(("git", "status"), ["git", "status", "--short"])
    assert not prefix_covers(("git", "status"), ["git", "-c", "core.pager=x", "status"])
    assert not prefix_covers(("git", "status"), ["gitk"])
    assert not prefix_covers(("git",), ["/usr/bin/git", "status"]), "an absolute path is a different token"
    assert not prefix_covers((), ["anything"]), "an empty prefix covers nothing"


def test_the_manifest_is_refused_at_write_when_the_host_does_not_allow_it(tmp_path: Path) -> None:
    """Same stance as a sandbox image: a 400 once, not a tool that fails on every call."""
    manifest = parse_manifest(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": "m"},
            "spec": {"shell_tools": [{"name": "run", "commands": ["git push"]}]},
        }
    )
    with pytest.raises(GovernanceError, match="git push"):
        validate_for_write(manifest, _settings(tmp_path, "git status"))


# ---------------------------------------------------------------------------
# What a call cannot do
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unlisted_command_is_refused_before_it_runs(tmp_path: Path) -> None:
    marker = tmp_path / "touched"
    tool = _tool(tmp_path, commands=["git status"])
    out = await _run(tool, tmp_path, {"argv": ["touch", str(marker)]})
    assert "shell_error" in tool_output_content(out)
    assert not marker.exists()


@pytest.mark.asyncio
async def test_an_option_before_the_subcommand_is_refused(tmp_path: Path) -> None:
    tool = _tool(tmp_path, commands=["git status"])
    out = await _run(tool, tmp_path, {"argv": ["git", "-c", "core.pager=cat", "status"]})
    assert "shell_error" in tool_output_content(out)


@pytest.mark.asyncio
async def test_the_operator_allowlist_is_checked_per_call_too(tmp_path: Path) -> None:
    """A manifest bound when the host allowed `git` is still refused if the host no longer does."""
    tool = _tool(tmp_path, commands=["git status"], allowed="git")
    out = await _run(tool, tmp_path, {"argv": ["git", "status"]}, allowed="uv run ruff")
    assert "FELIX_SHELL_ALLOWED_COMMANDS" in tool_output_content(out)


@pytest.mark.asyncio
async def test_shell_metacharacters_are_arguments_not_syntax(tmp_path: Path) -> None:
    tool = _tool(tmp_path, commands=["echo"])
    res = _result(await _run(tool, tmp_path, {"argv": ["echo", "a", "&&", "id", ";", "whoami", "|", "cat"]}))
    assert res["exit_code"] == 0
    assert res["stdout"].strip() == "a && id ; whoami | cat"


@pytest.mark.asyncio
async def test_cwd_cannot_leave_the_workspace(tmp_path: Path) -> None:
    tool = _tool(tmp_path, commands=["pwd"])
    out = await _run(tool, tmp_path, {"argv": ["pwd"], "cwd": ".."})
    assert "escapes workspace root" in tool_output_content(out)
    out = await _run(tool, tmp_path, {"argv": ["pwd"], "cwd": "/"})
    assert "absolute paths" in tool_output_content(out)
    (tmp_path / "sub").mkdir()
    res = _result(await _run(tool, tmp_path, {"argv": ["pwd"], "cwd": "sub"}))
    assert res["cwd"] == "sub"
    assert Path(res["stdout"].strip()).resolve() == (tmp_path / "sub").resolve()


@pytest.mark.asyncio
async def test_the_child_does_not_see_the_api_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_MCP_TOKEN", "ghp_secret")
    monkeypatch.setenv("FELIX_ANTHROPIC_API_KEY", "sk-ant-secret")
    monkeypatch.setenv("FELIX_DATABASE_URL", "postgresql://u:p@db/felix")
    tool = _tool(tmp_path, commands=[PY])
    res = _result(
        await _run(
            tool, tmp_path, {"argv": [PY, "-c", "import os, json; print(json.dumps(dict(os.environ)))"]}
        )
    )
    env = json.loads(res["stdout"])
    assert "GITHUB_MCP_TOKEN" not in env
    assert not any(k.startswith("FELIX_") for k in env), sorted(env)
    assert "PATH" in env, "an allowlisted binary must still be able to resolve itself"
    # macOS launchd adds `__CF_USER_TEXT_ENCODING` to every process it spawns; it is not ours.
    assert set(env) - {"__CF_USER_TEXT_ENCODING"} <= {"PATH", "HOME", "LANG", "LC_ALL", "TZ"}, sorted(env)


@pytest.mark.asyncio
async def test_a_run_is_killed_at_its_timeout(tmp_path: Path) -> None:
    tool = _tool(tmp_path, commands=[PY], timeout_ms=1000)
    res = _result(await _run(tool, tmp_path, {"argv": [PY, "-c", "import time; time.sleep(30)"]}))
    assert res["timed_out"] is True
    assert res["duration_ms"] < 10_000


@pytest.mark.asyncio
async def test_output_is_capped_and_marked(tmp_path: Path) -> None:
    tool = _tool(tmp_path, commands=[PY])
    code = f"print('x' * {MAX_OUTPUT_BYTES * 3})"
    res = _result(await _run(tool, tmp_path, {"argv": [PY, "-c", code]}))
    assert res["truncated"] is True
    assert len(res["stdout"].encode()) <= MAX_OUTPUT_BYTES


@pytest.mark.asyncio
async def test_exit_code_stderr_and_stdin_are_reported(tmp_path: Path) -> None:
    tool = _tool(tmp_path, commands=[PY])
    code = (
        "import sys; data = sys.stdin.read(); print(data.upper()); print('bad', file=sys.stderr); sys.exit(3)"
    )
    res = _result(await _run(tool, tmp_path, {"argv": [PY, "-c", code], "stdin": "hello"}))
    assert res["exit_code"] == 3
    assert res["stdout"].strip() == "HELLO"
    assert res["stderr"].strip() == "bad"


# ---------------------------------------------------------------------------
# The wiring — pin what makes the governance stack see this tool
# ---------------------------------------------------------------------------


def test_shell_is_an_execution_transport_and_not_a_trusted_one() -> None:
    """`_EXECUTION_TRANSPORTS` is what makes command screening read *every* string argument;
    `_TRUSTED_TRANSPORTS` is what would exempt the output from content screening."""
    assert "shell" in builder._EXECUTION_TRANSPORTS
    assert "shell" not in builder._TRUSTED_TRANSPORTS


@pytest.mark.asyncio
async def test_command_screening_sees_argv(tmp_path: Path) -> None:
    """A destructive rm reaches the default screening rules through argv, not a `command` key."""
    from felix.manifests.schema import CommandScreening

    tool = _tool(tmp_path, commands=["rm"])
    screened = builder.apply_command_screening(
        [tool], CommandScreening(enabled=True, include_defaults=True), "m"
    )[0]
    out = await _run(screened, tmp_path, {"argv": ["rm", "-rf", "/"]}, allowed="rm")
    assert is_wrapper_deny(out), tool_output_content(out)
    assert out.metadata.get("source") == "command"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_target_tools_that_miss_the_shell_tool_still_screen_it(tmp_path: Path) -> None:
    """`command_screening.target_tools` narrows screening for ordinary tools. An execution
    transport is screened regardless — the literal set that decided this was updated for
    sandbox and container and not for shell, which is the gap this pins."""
    from felix.manifests.schema import CommandScreening

    tool = _tool(tmp_path, commands=["rm"])
    screening = CommandScreening(enabled=True, include_defaults=True, target_tools=["some_other_tool"])
    screened = builder.apply_command_screening([tool], screening, "m")[0]
    out = await _run(screened, tmp_path, {"argv": ["rm", "-rf", "/"]}, allowed="rm")
    assert is_wrapper_deny(out), tool_output_content(out)


def test_the_bound_tool_carries_the_shell_transport(tmp_path: Path) -> None:
    tool = _tool(tmp_path, commands=["git status", "./scripts/test.sh"])
    assert tool.name == "run"
    assert tool.executor.transport == "shell"
    assert "git status" in tool.description and "./scripts/test.sh" in tool.description
