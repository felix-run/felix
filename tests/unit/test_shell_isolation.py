"""Outside development, a shell tool runs in a shell runner or a sandbox, never in the API.

A shell tool runs repository code: `make test` imports whatever the agent just wrote. Run as the
API's child, that code can read the API's environment through `/proc` — the model keys, the
GitHub token. The separate runner existed (`FELIX_SHELL_RUNNER_URL`), but nothing required it, so
setting `FELIX_SHELL_ALLOWED_COMMANDS` alone on a production host — the obvious fix for
`shell tools are disabled` — would have made every command run in the API's own container.

Held at three places, each pinned here: the boot (`validate_runtime`), the manifest write and
compile (`assert_shell_commands_allowed`), and the call itself, for settings that never booted.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from felix.config import Settings
from felix.context import AuthContext, RequestContext, async_run_with_context
from felix.manifests.schema import ShellToolRef
from felix.security.shell_policy import (
    ShellNotAllowedError,
    assert_shell_commands_allowed,
    exec_is_isolated,
    isolation_refusal,
)
from felix.tools.errors import ToolErrorCode, read_tool_error_code
from felix.tools.shell import tools_from_shell_refs
from felix.tools.types import ToolInvocationCtx, tool_output_content

PY = sys.executable
TOKEN = "t" * 64


def _settings(ws: Path, **kw: Any) -> Settings:
    base: dict[str, Any] = {"workspace_root": str(ws), "allow_insecure": True, "shell_allowed_commands": PY}
    return Settings(**{**base, **kw})


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_a_deployment_with_shell_tools_and_no_runner_refuses_to_boot(
    tmp_path: Path, environment: str
) -> None:
    with pytest.raises(RuntimeError, match="FELIX_SHELL_RUNNER_URL"):
        _settings(tmp_path, environment=environment)._validate_shell_isolation()


def test_a_runner_satisfies_it(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path, environment="production", shell_runner_url="http://shell:8080", shell_runner_token=TOKEN
    )

    settings._validate_shell_isolation()
    assert exec_is_isolated(settings)


def test_the_hosted_workspace_backend_satisfies_it(tmp_path: Path) -> None:
    """Its sandbox is where the command runs; `_validate_workspace_gateway` checks it separately."""
    settings = _settings(tmp_path, environment="production", workspace_backend="hosted")

    assert isolation_refusal(settings) is None


def test_development_may_still_exec_locally_and_no_shell_needs_nothing(tmp_path: Path) -> None:
    _settings(tmp_path, environment="development")._validate_shell_isolation()
    _settings(tmp_path, environment="production", shell_allowed_commands="")._validate_shell_isolation()


def test_the_validate_runtime_call_runs_the_check(tmp_path: Path) -> None:
    """The private check is only worth something if boot calls it."""
    with pytest.raises(RuntimeError, match="FELIX_SHELL_RUNNER_URL"):
        _settings(tmp_path, environment="production", auth_mode="none").validate_runtime()


def test_a_manifest_with_a_shell_tool_is_refused_at_write_without_a_runner(tmp_path: Path) -> None:
    ref = ShellToolRef(name="run", commands=[PY])

    with pytest.raises(ShellNotAllowedError, match=r"shell_tools\[run\]: shell tools need a shell runner"):
        assert_shell_commands_allowed([ref], _settings(tmp_path, environment="production"))
    assert_shell_commands_allowed([ref], _settings(tmp_path, environment="development"))


def test_the_disabled_message_says_a_runner_is_needed_too_but_only_outside_development(
    tmp_path: Path,
) -> None:
    """What production showed: the allowlist alone would not have been enough, and now it says so."""
    ref = ShellToolRef(name="run", commands=[PY])

    with pytest.raises(ShellNotAllowedError) as prod:
        assert_shell_commands_allowed(
            [ref], _settings(tmp_path, environment="production", shell_allowed_commands="")
        )
    with pytest.raises(ShellNotAllowedError) as dev:
        assert_shell_commands_allowed(
            [ref], _settings(tmp_path, environment="development", shell_allowed_commands="")
        )

    assert "FELIX_SHELL_RUNNER_URL" in str(prod.value)
    assert "FELIX_SHELL_RUNNER_URL" not in str(dev.value)


async def test_the_call_itself_refuses_a_local_exec_outside_development(tmp_path: Path) -> None:
    """Settings that never went through `validate_runtime` still cannot exec in this process."""
    marker = tmp_path / "ran"
    dev = _settings(tmp_path, environment="development")
    tool = tools_from_shell_refs([ShellToolRef(name="run", commands=[PY])], settings=dev)[0]
    prod = _settings(tmp_path, environment="production")

    ctx = RequestContext(settings=prod, auth=AuthContext(tenant_id="t"), manifest_id="m", thread_id="th")
    async with async_run_with_context(ctx):
        out = await tool.executor.execute(
            {"argv": [PY, "-c", f"open({str(marker)!r}, 'w').write('x')"]}, ToolInvocationCtx()
        )

    assert read_tool_error_code(out) is ToolErrorCode.PERMISSION_DENIED, tool_output_content(out)
    assert "shell runner" in tool_output_content(out)
    assert not marker.exists(), "the command ran in the API's process"


async def test_the_hosted_backends_deployment_scope_cannot_exec_in_the_api(tmp_path: Path) -> None:
    """Found in review. The hosted backend serves a `deployment` scope from this host, so counting
    "hosted" as isolated let that scope's shell run as the API's child in production — boot, write
    and compile all passed. The call refuses on where it is about to exec, not on the settings."""
    from felix.tools.workspace_scope import bound_scope

    marker = tmp_path / "ran"
    prod = _settings(tmp_path, environment="production", workspace_backend="hosted")
    tool = tools_from_shell_refs([ShellToolRef(name="run", commands=[PY])], settings=_settings(tmp_path))[0]

    ctx = RequestContext(
        settings=prod, auth=AuthContext(tenant_id="default"), manifest_id="m", thread_id="th"
    )
    async with async_run_with_context(ctx):
        with bound_scope("deployment"):
            out = await tool.executor.execute(
                {"argv": [PY, "-c", f"open({str(marker)!r}, 'w').write('x')"]}, ToolInvocationCtx()
            )

    assert read_tool_error_code(out) is ToolErrorCode.PERMISSION_DENIED, tool_output_content(out)
    assert not marker.exists(), "the deployment scope's command ran in the API's process"


def test_a_deployment_scope_shell_under_hosted_needs_a_runner_at_write(tmp_path: Path) -> None:
    ref = ShellToolRef(name="run", commands=[PY])
    hosted = _settings(tmp_path, environment="production", workspace_backend="hosted")

    assert_shell_commands_allowed([ref], hosted, scope="thread")
    with pytest.raises(ShellNotAllowedError, match="scope: deployment"):
        assert_shell_commands_allowed([ref], hosted, scope="deployment")
    runner = _settings(
        tmp_path, environment="production", workspace_backend="hosted", shell_runner_url="http://s:1"
    )
    assert_shell_commands_allowed([ref], runner, scope="deployment")


def test_development_alone_is_not_a_development_box(tmp_path: Path) -> None:
    """Compose defaults FELIX_ENVIRONMENT to development; its auth is api_key. That is a deployment
    that forgot the variable, not a laptop, and it gets the refusal."""
    with pytest.raises(RuntimeError, match="FELIX_SHELL_RUNNER_URL"):
        _settings(tmp_path, environment="development", auth_mode="api_key")._validate_shell_isolation()
