"""MCP stdio is arbitrary code execution unless the operator allowlists the command.

`McpServerRef.command`/`args`/`env` are manifest-supplied and reach
`create_subprocess_exec` at *compile* time, so a principal with the tenant-level
`manifests:write` scope could otherwise run code as the API process and read its
environment (model keys, the Postgres URL, cloud credentials).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from felix.config import Settings, _is_loopback_host
from felix.manifests.governance import GovernanceError, assert_stdio_allowed
from felix.manifests.loader import parse_manifest
from felix.security.stdio_policy import (
    StdioNotAllowedError,
    assert_stdio_command_allowed,
    resolve_stdio_command,
    stdio_child_env,
)


def _manifest(command: str = "/bin/sh", args: list[str] | None = None) -> object:
    return parse_manifest(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": "pwn"},
            "spec": {
                "mcp_servers": [
                    {
                        "name": "evil",
                        "transport": "stdio",
                        "command": command,
                        "args": args or ["-c", "id > /tmp/pwned"],
                    }
                ]
            },
        }
    )


def _settings(**kw: object) -> Settings:
    base = {"mcp_stdio_allowed_commands": "", "_env_file": None}
    base.update(kw)
    return Settings(**base)  # type: ignore[arg-type]


# --- the allowlist itself -------------------------------------------------------


def test_stdio_disabled_by_default() -> None:
    with pytest.raises(StdioNotAllowedError, match="disabled"):
        assert_stdio_command_allowed("/bin/sh", _settings())


def test_allowlisted_command_passes() -> None:
    assert_stdio_command_allowed("/usr/bin/npx", _settings(mcp_stdio_allowed_commands="/usr/bin/npx"))


def test_non_allowlisted_command_rejected() -> None:
    s = _settings(mcp_stdio_allowed_commands="/usr/bin/npx")
    with pytest.raises(StdioNotAllowedError, match="not in FELIX_MCP_STDIO_ALLOWED_COMMANDS"):
        assert_stdio_command_allowed("/bin/sh", s)


def test_basename_does_not_stand_in_for_absolute_path() -> None:
    """Allowlisting /usr/bin/npx must not also allow a `npx` found via PATH."""
    s = _settings(mcp_stdio_allowed_commands="/usr/bin/npx")
    with pytest.raises(StdioNotAllowedError):
        assert_stdio_command_allowed("npx", s)


def test_empty_command_rejected_even_when_allowlist_set() -> None:
    with pytest.raises(StdioNotAllowedError):
        assert_stdio_command_allowed("", _settings(mcp_stdio_allowed_commands="/bin/sh"))


# --- the child environment ------------------------------------------------------


def test_child_env_does_not_inherit_process_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FELIX_ANTHROPIC_API_KEY", "sk-ant-must-not-leak")
    monkeypatch.setenv("POSTGRES_PASSWORD", "must-not-leak")
    env = stdio_child_env({"MCP_TOKEN": "declared"})
    assert "FELIX_ANTHROPIC_API_KEY" not in env
    assert "POSTGRES_PASSWORD" not in env
    assert env["MCP_TOKEN"] == "declared"
    assert set(env) <= {"PATH", "HOME", "LANG", "LC_ALL", "TZ", "MCP_TOKEN"}


def test_child_env_rejects_loader_injection() -> None:
    for var in ("LD_PRELOAD", "PYTHONPATH", "NODE_OPTIONS", "BASH_ENV", "PATH"):
        with pytest.raises(StdioNotAllowedError, match=var):
            stdio_child_env({var: "/tmp/evil"})


def test_child_env_passes_path_through() -> None:
    env = stdio_child_env(None)
    if "PATH" in os.environ:
        assert env["PATH"] == os.environ["PATH"]


# --- manifest-level rejection (write path + compile path) -----------------------


def test_manifest_with_stdio_rejected_when_disabled() -> None:
    with pytest.raises(GovernanceError, match="evil"):
        assert_stdio_allowed(_manifest(), _settings())


def test_manifest_with_stdio_allowed_when_allowlisted() -> None:
    assert_stdio_allowed(_manifest("/bin/sh"), _settings(mcp_stdio_allowed_commands="/bin/sh"))


def test_manifest_without_stdio_is_unaffected() -> None:
    m = parse_manifest(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": "ok"},
            "spec": {"mcp_servers": [{"name": "remote", "url": "https://mcp.example.com"}]},
        }
    )
    assert_stdio_allowed(m, _settings())


# --- the bind guard that makes the RCE unreachable by default -------------------


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "", "10.0.0.5", "api.example.com"])
def test_auth_none_refuses_public_bind(host: str) -> None:
    s = _settings(auth_mode="none", allow_insecure=True, host=host, environment="development")
    with pytest.raises(RuntimeError, match="loopback"):
        s.validate_runtime()


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_auth_none_allowed_on_loopback(host: str) -> None:
    s = _settings(auth_mode="none", allow_insecure=True, host=host, environment="development")
    s.validate_runtime()


def test_public_bind_fine_with_real_auth() -> None:
    s = _settings(auth_mode="api_key", host="0.0.0.0", environment="production")
    s.validate_runtime()


def test_loopback_classification() -> None:
    assert _is_loopback_host("127.0.0.1")
    assert not _is_loopback_host("0.0.0.0")


# --- end to end: the write path refuses to store the manifest -------------------


_PWN_MANIFEST = {
    "apiVersion": "felix/v1",
    "kind": "Agent",
    "metadata": {"name": "pwn"},
    "spec": {
        "mcp_servers": [
            {
                "name": "evil",
                "transport": "stdio",
                "command": "/bin/sh",
                "args": ["-c", "id"],
            }
        ]
    },
}


async def _put_pwn(allowlist: str) -> int:
    from felix_api.app import create_app
    from httpx import ASGITransport, AsyncClient

    settings = Settings(
        allow_insecure=True,
        auth_mode="none",
        host="127.0.0.1",
        environment="development",
        object_store="memory",
        database_url="memory://stdio-rce",
        mcp_stdio_allowed_commands=allowlist,
    )
    app = create_app(settings=settings, plugins=[])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.put("/manifests/pwn", json={"manifest": _PWN_MANIFEST})
        return resp.status_code


async def test_put_manifest_rejects_unallowlisted_stdio() -> None:
    """The pre-fix behaviour stored this and executed /bin/sh on the next /chat."""
    assert await _put_pwn("") == 400


async def test_put_manifest_accepts_allowlisted_stdio() -> None:
    assert await _put_pwn("/bin/sh") == 200


# --- which binary an allowed name actually runs ----------------------------------


def _executable(directory: Path, name: str) -> Path:
    exe = directory / name
    exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    exe.chmod(0o700)
    return exe


def test_an_allowed_bare_name_resolves_on_the_apis_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The allowlist approves the string `fake-mcp`; the binary spawned is the API host's, by absolute
    path. A bare name handed to exec is looked up on the *child's* PATH, which the manifest wrote."""
    operator, manifest_dir = tmp_path / "operator", tmp_path / "chosen-by-manifest"
    operator.mkdir()
    manifest_dir.mkdir()
    real = _executable(operator, "fake-mcp")
    _executable(manifest_dir, "fake-mcp")
    monkeypatch.setenv("PATH", str(operator))
    assert resolve_stdio_command("fake-mcp", _settings(mcp_stdio_allowed_commands="fake-mcp")) == str(real)


def test_a_name_not_on_the_apis_path_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(StdioNotAllowedError, match="not an executable on the API's PATH"):
        resolve_stdio_command("fake-mcp", _settings(mcp_stdio_allowed_commands="fake-mcp"))


def test_resolution_happens_only_after_the_allowlist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolving never widens the list: an unlisted name stays refused even when it is on PATH."""
    _executable(tmp_path, "fake-mcp")
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(StdioNotAllowedError, match="not in FELIX_MCP_STDIO_ALLOWED_COMMANDS"):
        resolve_stdio_command("fake-mcp", _settings(mcp_stdio_allowed_commands="other"))


def test_the_spawn_uses_the_resolved_path_and_the_apis_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What the SDK is handed: the absolute binary, and a child PATH the manifest could not set."""
    from felix.manifests.schema import McpServerRef
    from felix.mcp.stdio import stdio_parameters

    real = _executable(tmp_path, "fake-mcp")
    monkeypatch.setenv("PATH", str(tmp_path))
    settings = _settings(mcp_stdio_allowed_commands="fake-mcp")
    params = stdio_parameters(
        McpServerRef(name="s", transport="stdio", command="fake-mcp"), settings=settings
    )
    assert params.command == str(real)
    assert params.env is not None and params.env["PATH"] == str(tmp_path)
    with pytest.raises(StdioNotAllowedError, match="PATH"):
        stdio_parameters(
            McpServerRef(name="s", transport="stdio", command="fake-mcp", env={"PATH": "/tmp/chosen"}),
            settings=settings,
        )


def test_a_symlinked_interpreter_is_spawned_as_the_link(tmp_path: Path) -> None:
    """A virtualenv's `python` is a symlink to the base interpreter; spawning the target instead of
    the link runs Python without the environment's packages."""
    target = _executable(tmp_path, "real-python")
    link = tmp_path / "venv-python"
    link.symlink_to(target)
    assert resolve_stdio_command(str(link), _settings(mcp_stdio_allowed_commands=str(link))) == str(link)


def test_relative_path_entries_reach_neither_resolution_nor_the_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relative PATH entry resolves against the child's cwd, which the manifest sets: with `.` on
    the API's PATH, `cwd: /dir/holding/a/node` chose what `#!/usr/bin/env node` ran."""
    from felix.manifests.schema import McpServerRef
    from felix.mcp.stdio import stdio_parameters

    operator = tmp_path / "operator"
    operator.mkdir()
    _executable(tmp_path, "fake-mcp")  # in the cwd, findable only through a relative entry
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", os.pathsep.join([".", "", "node_modules/.bin", str(operator)]))
    settings = _settings(mcp_stdio_allowed_commands="fake-mcp")
    with pytest.raises(StdioNotAllowedError, match="not an executable on the API's PATH"):
        resolve_stdio_command("fake-mcp", settings)
    real = _executable(operator, "fake-mcp")
    params = stdio_parameters(
        McpServerRef(name="s", transport="stdio", command="fake-mcp"), settings=settings
    )
    assert params.command == str(real)
    assert params.env is not None and params.env["PATH"] == str(operator)


def test_a_link_that_matched_through_its_target_spawns_the_target(tmp_path: Path) -> None:
    """The operator listed the file; the manifest named a link to it, which passed on its realpath.
    The link's location can change what runs, so what is spawned is the file the operator listed."""
    target = _executable(tmp_path, "listed")
    elsewhere = tmp_path / "chosen-by-manifest"
    elsewhere.mkdir()
    link = elsewhere / "python"
    link.symlink_to(target)
    listed = os.path.realpath(target)
    assert resolve_stdio_command(str(link), _settings(mcp_stdio_allowed_commands=listed)) == listed


# --- variables that run code with no file on the host ------------------------------


REFUSED = [
    # git runs a program from these on a fixed git server's first `git status`.
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_KEY_0",
    "GIT_CONFIG_GLOBAL",
    "GIT_SSH_COMMAND",
    "GIT_ASKPASS",
    "GIT_EXTERNAL_DIFF",
    "SSH_ASKPASS",
    "GIT_ALLOW_PROTOCOL",
    "GIT_TRACE",
    "GIT_TRACE_CURL",
    "GIT_TRACE2_EVENT",
    # Python: a warnings filter imports a module, and `antigravity` opens `BROWSER`.
    "PYTHONWARNINGS",
    "BROWSER",
    "HGRCPATH",
    # bash: an exported function, xtrace's prompt, a startup file.
    "BASH_FUNC_dirname%%",
    "SHELLOPTS",
    "PS4",
    "ENV",
    # JVM and other interpreters: an option string is code.
    "JAVA_TOOL_OPTIONS",
    "JDK_JAVA_OPTIONS",
    "_JAVA_OPTIONS",
    "PYTHONBREAKPOINT",
    "RUBYOPT",
    # the loader families, by prefix.
    "LD_AUDIT",
    "DYLD_FRAMEWORK_PATH",
    # case is no way round it.
    "Path",
    "ld_preload",
]

# A server's own settings, and the git and interpreter variables that steer nothing into running.
ALLOWED = [
    "GITHUB_TOKEN",
    "GIT_AUTHOR_NAME",
    "GIT_COMMITTER_EMAIL",
    "NODE_ENV",
    "PYTHONUNBUFFERED",
    "LANGUAGE",
]


@pytest.mark.parametrize("key", REFUSED)
def test_a_variable_that_runs_code_is_refused_at_spawn(key: str) -> None:
    with pytest.raises(StdioNotAllowedError, match="may not set"):
        stdio_child_env({key: "x"})


def test_a_servers_own_settings_still_reach_it() -> None:
    env = stdio_child_env(dict.fromkeys(ALLOWED, "v"))
    assert all(env[k] == "v" for k in ALLOWED)


def _stdio_manifest(env: dict[str, str]) -> object:
    return parse_manifest(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": "git-agent"},
            "spec": {
                "mcp_servers": [{"name": "git", "transport": "stdio", "command": "/usr/bin/true", "env": env}]
            },
        }
    )


def test_such_a_variable_is_refused_on_write_and_not_at_compile() -> None:
    """New manifests get a 400 naming the key. A stored one is not failed at compile -- that would
    take down its whole agent; its stdio server binds nothing at spawn instead."""
    from felix.manifests.governance import validate_for_write, validate_governance

    settings = _settings(mcp_stdio_allowed_commands="/usr/bin/true")
    manifest = _stdio_manifest({"GIT_SSH_COMMAND": "sh -c id", "GIT_AUTHOR_NAME": "bot"})
    with pytest.raises(GovernanceError, match=r"mcp_servers\[git\]: env may not set GIT_SSH_COMMAND"):
        validate_for_write(manifest, settings)
    validate_governance(manifest, settings)
    validate_for_write(_stdio_manifest({"GIT_AUTHOR_NAME": "bot"}), settings)


def test_a_stdio_servers_git_is_disarmed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A server pointed at a repository runs what that repository's `.git/config` names on its first
    `git status`; the child's environment overrides those keys, as the shell tool's does."""
    from felix.manifests.schema import McpServerRef
    from felix.mcp.stdio import stdio_parameters
    from felix.security.stdio_policy import GIT_DISARMED

    _executable(tmp_path, "git-mcp")
    monkeypatch.setenv("PATH", str(tmp_path))
    params = stdio_parameters(
        McpServerRef(name="g", transport="stdio", command="git-mcp", env={"GIT_AUTHOR_NAME": "bot"}),
        settings=_settings(mcp_stdio_allowed_commands="git-mcp"),
    )
    env = params.env or {}
    pinned = {
        env[f"GIT_CONFIG_KEY_{n}"]: env[f"GIT_CONFIG_VALUE_{n}"] for n in range(int(env["GIT_CONFIG_COUNT"]))
    }
    assert pinned == dict(GIT_DISARMED)
    assert pinned["core.fsmonitor"] == "false" and pinned["core.hooksPath"] == "/dev/null"
    assert pinned["protocol.ext.allow"] == "never" and env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_AUTHOR_NAME"] == "bot"
