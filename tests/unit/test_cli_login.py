"""`felix login`: what reaches stdout, what reaches stderr, the exit codes, and `--save`.

The flows themselves are `felix_client.github_device_login` and `github_actions_login`, covered
against the real routes in `tests/e2e/test_github_login_client.py` and
`tests/e2e/test_github_actions_login_client.py`; here they are replaced so the command's own
wiring is the thing under test.
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


def test_insecure_is_passed_through_and_off_by_default(calls: list[dict[str, Any]]) -> None:
    assert _run().exit_code == 0
    assert _run("--insecure").exit_code == 0
    assert [c["allow_insecure"] for c in calls] == [False, True]


def test_a_verification_url_off_github_is_called_out(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_login(url: str, **kwargs: Any) -> LoginToken:
        kwargs["on_code"](
            DeviceCode(
                user_code="X", verification_uri="https://github.evil.example/x", expires_in=900, interval=5
            )
        )
        return _token()

    monkeypatch.setattr(client_login, "github_device_login", fake_login)
    result = _run()
    assert "not on github.com" in result.stderr


def test_the_real_github_url_draws_no_warning(calls: list[dict[str, Any]]) -> None:
    assert "warning" not in _run().stderr


def test_a_token_directory_others_can_reach_is_refused(
    calls: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    (tmp_path / "felix").mkdir(mode=0o700)
    (tmp_path / "felix").chmod(0o777)
    result = _run("--save")
    assert result.exit_code == 1
    assert "not saved" in result.stderr
    assert not (tmp_path / "felix" / "token").exists()


# --- --github-actions ---------------------------------------------------------------------


@pytest.fixture
def actions_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []

    async def fake_actions(url: str, **kwargs: Any) -> LoginToken:
        seen.append({"url": url, **kwargs})
        return _token()

    async def no_device(url: str, **kwargs: Any) -> LoginToken:
        raise AssertionError("--github-actions must not start a device flow")

    monkeypatch.setattr(client_login, "github_actions_login", fake_actions)
    monkeypatch.setattr(client_login, "github_device_login", no_device)
    return seen


def test_github_actions_trades_the_job_token_and_prints_only_the_token(
    actions_calls: list[dict[str, Any]],
) -> None:
    result = _run(
        "--github-actions", "--url", "https://felix.example", "--tenant", "ops", "--audience", "aud"
    )
    assert result.exit_code == 0, result.output
    assert result.stdout == "eyJ.token.sig\n"
    assert actions_calls == [
        {"url": "https://felix.example", "audience": "aud", "tenant": "ops", "allow_insecure": False}
    ]


def test_github_actions_audience_defaults_to_none_so_the_client_uses_the_url(
    actions_calls: list[dict[str, Any]],
) -> None:
    assert _run("--github-actions").exit_code == 0
    assert actions_calls[0]["audience"] is None


def test_audience_without_github_actions_is_a_usage_error(calls: list[dict[str, Any]]) -> None:
    result = _run("--audience", "x")
    assert result.exit_code == 2
    assert "--github-actions" in result.stderr
    assert calls == []


def test_an_ambiguous_workflow_is_told_it_is_the_workflow(monkeypatch: pytest.MonkeyPatch) -> None:
    async def ambiguous(url: str, **kwargs: Any) -> LoginToken:
        raise LoginError("tenant_ambiguous", "m", status=409, tenants=("acme", "ops"))

    monkeypatch.setattr(client_login, "github_actions_login", ambiguous)
    result = _run("--github-actions")
    assert result.exit_code == 2
    assert "This workflow is granted more than one tenant (acme, ops)" in result.stderr


def test_github_actions_passes_insecure_through(actions_calls: list[dict[str, Any]]) -> None:
    assert _run("--github-actions", "--insecure").exit_code == 0
    assert actions_calls[0]["allow_insecure"] is True


def test_github_actions_outside_a_job_exits_1_with_its_code(monkeypatch: pytest.MonkeyPatch) -> None:
    async def outside(url: str, **kwargs: Any) -> LoginToken:
        raise LoginError("not_in_github_actions", "ACTIONS_ID_TOKEN_REQUEST_URL is not set", status=0)

    monkeypatch.setattr(client_login, "github_actions_login", outside)
    result = _run("--github-actions")
    assert result.exit_code == 1
    assert "login failed (not_in_github_actions)" in result.stderr
    assert result.stdout == ""


def test_in_a_job_the_token_is_masked_on_stderr_before_it_is_printed(
    actions_calls: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    result = _run("--github-actions")
    assert "::add-mask::eyJ.token.sig" in result.stderr
    # stdout stays the token alone, so `TOKEN=$(felix login --github-actions)` captures just it.
    assert result.stdout == "eyJ.token.sig\n"


def test_outside_a_job_nothing_is_masked(
    actions_calls: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    assert "::add-mask::" not in _run("--github-actions").stderr


def test_an_audience_naming_another_server_is_called_out(actions_calls: list[dict[str, Any]]) -> None:
    other = _run("--github-actions", "--url", "https://felix.example", "--audience", "https://prod.example")
    same = _run("--github-actions", "--url", "https://felix.example", "--audience", "https://felix.example/")
    assert "is being sent to https://felix.example" in other.stderr
    assert "warning" not in same.stderr
