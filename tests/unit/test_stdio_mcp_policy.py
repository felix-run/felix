"""MCP stdio is arbitrary code execution unless the operator allowlists the command.

`McpServerRef.command`/`args`/`env` are manifest-supplied and reach
`create_subprocess_exec` at *compile* time, so a principal with the tenant-level
`manifests:write` scope could otherwise run code as the API process and read its
environment (model keys, the Postgres URL, cloud credentials).
"""

from __future__ import annotations

import os

import pytest
from felix.config import Settings, _is_loopback_host
from felix.manifests.governance import GovernanceError, assert_stdio_allowed
from felix.manifests.loader import parse_manifest
from felix.security.stdio_policy import (
    StdioNotAllowedError,
    assert_stdio_command_allowed,
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
    for var in ("LD_PRELOAD", "PYTHONPATH", "NODE_OPTIONS", "BASH_ENV"):
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


@pytest.mark.asyncio
async def test_put_manifest_rejects_unallowlisted_stdio() -> None:
    """The pre-fix behaviour stored this and executed /bin/sh on the next /chat."""
    assert await _put_pwn("") == 400


@pytest.mark.asyncio
async def test_put_manifest_accepts_allowlisted_stdio() -> None:
    assert await _put_pwn("/bin/sh") == 200
