"""`spec.shell_tools` — what the governed shell must not be able to do.

Each test names one thing the module docstring of `felix/tools/shell.py` promises the tool
cannot do, and proves it against a real subprocess: there is no fake here, because the thing
under test is the boundary between the model's argv and the host.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
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
from felix.tools import shell as shell_mod
from felix.tools.errors import ToolErrorCode, read_tool_error_code
from felix.tools.shell import MAX_OUTPUT_BYTES, MAX_TOTAL_OUTPUT_BYTES, ShellArgs, tools_from_shell_refs
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
    assert read_tool_error_code(out) is None, text
    return json.loads(text)


def _refused(out: Any) -> str:
    """The refusal text, having asserted the output is a counted permission_denied tool error."""
    assert read_tool_error_code(out) == ToolErrorCode.PERMISSION_DENIED, tool_output_content(out)
    return tool_output_content(out)


# A child that writes a heartbeat file forever; "is it dead" is "did the heartbeat stop".
_HEARTBEAT = "import time, pathlib, sys; p = pathlib.Path(sys.argv[1]); [p.write_text(str(time.time())) or time.sleep(0.05) for _ in iter(int, 1)]"


def _heartbeat_stopped(path: Path) -> bool:
    if not path.exists():
        return True
    first = path.read_text()
    time.sleep(0.4)
    return path.read_text() == first


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
    _refused(out)
    assert not marker.exists()


@pytest.mark.asyncio
async def test_an_option_before_the_subcommand_is_refused(tmp_path: Path) -> None:
    tool = _tool(tmp_path, commands=["git status"])
    out = await _run(tool, tmp_path, {"argv": ["git", "-c", "core.pager=cat", "status"]})
    _refused(out)


@pytest.mark.asyncio
async def test_the_operator_allowlist_is_checked_per_call_too(tmp_path: Path) -> None:
    """A manifest bound when the host allowed `git` is still refused if the host no longer does."""
    tool = _tool(tmp_path, commands=["git status"], allowed="git")
    out = await _run(tool, tmp_path, {"argv": ["git", "status"]}, allowed="uv run ruff")
    assert "FELIX_SHELL_ALLOWED_COMMANDS" in _refused(out)


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
    assert "escapes workspace root" in _refused(out)
    out = await _run(tool, tmp_path, {"argv": ["pwd"], "cwd": "/"})
    assert "absolute paths" in _refused(out)
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
async def test_a_timed_out_command_takes_its_children_with_it(tmp_path: Path) -> None:
    """The documented use — a test script that spawns pytest — is a process *tree*.

    Killing only the direct child leaves a grandchild holding the pipes, and the tool would
    wait on them forever. The group is killed, and the grandchild's heartbeat stops.
    """
    beat = tmp_path / "beat"
    parent = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {_HEARTBEAT!r}, {str(beat)!r}]); time.sleep(60)"
    )
    tool = _tool(tmp_path, commands=[PY], timeout_ms=1000)
    res = _result(await _run(tool, tmp_path, {"argv": [PY, "-c", parent]}))
    assert res["timed_out"] is True
    assert res["duration_ms"] < 10_000, "the tool must not wait on an orphan's pipe"
    assert _heartbeat_stopped(beat), "the grandchild outlived the timeout"


@pytest.mark.asyncio
async def test_output_past_the_budget_kills_the_command(tmp_path: Path) -> None:
    """`MAX_OUTPUT_BYTES` bounds what the model sees; `MAX_TOTAL_OUTPUT_BYTES` bounds the API."""
    tool = _tool(tmp_path, commands=[PY], timeout_ms=60_000)
    code = "import sys\nwhile True:\n    sys.stdout.write('x' * 65536)"
    res = _result(await _run(tool, tmp_path, {"argv": [PY, "-c", code]}))
    assert res["output_exceeded"] is True
    assert res["truncated"] is True
    assert res["timed_out"] is False, "the budget, not the clock, ended it"
    assert len(res["stdout"].encode()) <= MAX_OUTPUT_BYTES
    assert res["duration_ms"] < 30_000
    assert MAX_TOTAL_OUTPUT_BYTES > MAX_OUTPUT_BYTES


@pytest.mark.asyncio
async def test_cancelling_the_call_kills_the_command(tmp_path: Path) -> None:
    """A client that disconnects cancels the task; nothing it spawned may survive that."""
    beat = tmp_path / "beat"
    tool = _tool(tmp_path, commands=[PY], timeout_ms=60_000)
    task = asyncio.create_task(_run(tool, tmp_path, {"argv": [PY, "-c", _HEARTBEAT, str(beat)]}))
    await asyncio.sleep(0.5)
    assert beat.exists(), "the child never started"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _heartbeat_stopped(beat), "the child outlived the cancelled call"


@pytest.mark.asyncio
async def test_a_refusal_is_counted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    counted: list[tuple[str, dict[str, str]]] = []
    monkeypatch.setattr(
        shell_mod, "record_counter", lambda name, labels: counted.append((name, dict(labels)))
    )
    tool = _tool(tmp_path, commands=["git status"])
    _refused(await _run(tool, tmp_path, {"argv": ["id"]}))
    _refused(await _run(tool, tmp_path, {"argv": ["git", "status"], "cwd": ".."}))
    assert counted == [
        ("felix_shell_denied", {"tool": "run", "reason": "argv"}),
        ("felix_shell_denied", {"tool": "run", "reason": "cwd"}),
    ]


@pytest.mark.asyncio
async def test_relative_path_entries_do_not_reach_the_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relative PATH entry resolves against the cwd the model chose."""
    monkeypatch.setenv("PATH", os.pathsep.join(["/usr/bin", ".", "node_modules/.bin", "/bin"]))
    tool = _tool(tmp_path, commands=[PY])
    res = _result(await _run(tool, tmp_path, {"argv": [PY, "-c", "import os; print(os.environ['PATH'])"]}))
    assert res["stdout"].strip().split(os.pathsep) == ["/usr/bin", "/bin"]


def test_argv_is_bounded() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ShellArgs(argv=["x"] * 257)
    with pytest.raises(ValidationError, match="bytes"):
        ShellArgs(argv=["x" * 70_000])
    assert ShellArgs(argv=["x"] * 256).argv


def test_a_shell_tool_may_not_be_reached_anonymously_outside_development(tmp_path: Path) -> None:
    """The cowork precedent as a rule: anonymous callers make the gating approvals anonymous."""
    spec = {
        "shell_tools": [{"name": "run", "commands": ["git status"]}],
        "auth": {"inbound": {"allow_anonymous": True}},
    }
    manifest = parse_manifest(
        {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": "m"}, "spec": spec}
    )
    prod = Settings(
        workspace_root=str(tmp_path), shell_allowed_commands="git status", environment="production"
    )
    with pytest.raises(GovernanceError, match="allow_anonymous"):
        validate_for_write(manifest, prod)
    dev = Settings(
        workspace_root=str(tmp_path), shell_allowed_commands="git status", environment="development"
    )
    validate_for_write(manifest, dev)


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
