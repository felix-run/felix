"""`felix login`: what reaches stdout, what reaches stderr, the exit codes, and `--save`.

The flow itself is `felix_client.github_device_login`, covered against the real routes in
`tests/e2e/test_github_login_client.py`; here it is replaced so the command's own wiring is the
thing under test.
"""

from __future__ import annotations

import stat
import time
from pathlib import Path
from typing import Any

import pytest
from felix_cli.main import app
from felix_client import login as client_login
from felix_client.login import DeviceCode, LoginError, LoginToken
from typer.testing import CliRunner

_CODE = DeviceCode(
    user_code="ABCD-1234", verification_uri="https://github.com/login/device", expires_in=900, interval=5
)


def _token() -> LoginToken:
    return LoginToken(
        access_token="eyJ.token.sig",
        tenant="acme",
        scopes=("jobs:read",),
        expires_at=time.time() + 3600,
        base_url="http://localhost:8080",
    )


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []

    async def fake_login(url: str, **kwargs: Any) -> LoginToken:
        seen.append({"url": url, **kwargs})
        kwargs["on_code"](_CODE)
        return _token()

    monkeypatch.setattr(client_login, "github_device_login", fake_login)
    return seen


def _run(*args: str) -> Any:
    return CliRunner().invoke(app, ["login", *args])


def test_stdout_is_the_token_alone(calls: list[dict[str, Any]]) -> None:
    result = _run("--url", "https://felix.example")
    assert result.exit_code == 0, result.output
    assert result.stdout == "eyJ.token.sig\n"
    assert "ABCD-1234" in result.stderr and "https://github.com/login/device" in result.stderr
    assert calls[0]["url"] == "https://felix.example"
    assert calls[0]["tenant"] is None


def test_tenant_is_passed_through(calls: list[dict[str, Any]]) -> None:
    assert _run("--tenant", "globex").exit_code == 0
    assert calls[0]["tenant"] == "globex"


def test_save_writes_the_token_file_and_prints_no_token(
    calls: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    result = _run("--save")
    saved = tmp_path / "felix" / "token"
    assert result.exit_code == 0, result.output
    assert result.stdout == ""
    assert "eyJ.token.sig" in saved.read_text()
    assert stat.S_IMODE(saved.stat().st_mode) == 0o600


def _refusing(monkeypatch: pytest.MonkeyPatch, exc: LoginError) -> None:
    async def fake_login(url: str, **kwargs: Any) -> LoginToken:
        raise exc

    monkeypatch.setattr(client_login, "github_device_login", fake_login)


def test_ambiguous_membership_names_the_tenants_and_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
    _refusing(monkeypatch, LoginError("tenant_ambiguous", "m", status=409, tenants=("acme", "globex")))
    result = _run()
    assert result.exit_code == 2
    assert "acme, globex" in result.stderr and "--tenant" in result.stderr
    assert result.stdout == ""


def test_any_other_refusal_exits_1_with_its_code(monkeypatch: pytest.MonkeyPatch) -> None:
    _refusing(monkeypatch, LoginError("not_a_member", "not an active member", status=403))
    result = _run()
    assert result.exit_code == 1
    assert "not_a_member" in result.stderr
    assert result.stdout == ""
