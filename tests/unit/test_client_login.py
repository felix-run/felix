"""`felix_client.login` without a server: the saved token file, the deadline, the network.

The happy path and every server refusal run against the real routes in
`tests/e2e/test_github_login_client.py`. What is here is what no server answer can reach.
"""

from __future__ import annotations

import json
import stat
import time
from pathlib import Path

import httpx
import pytest
from felix_client.login import LoginError, LoginToken, github_device_login, load_token, save_token, token_path

_BASE = "http://felix.test"


def _token(**overrides: object) -> LoginToken:
    fields: dict[str, object] = {
        "access_token": "a.b.c",
        "tenant": "acme",
        "scopes": ("jobs:read",),
        "expires_at": time.time() + 3600,
        "base_url": _BASE,
    }
    fields.update(overrides)
    return LoginToken(**fields)  # type: ignore[arg-type]


# --- the saved token ----------------------------------------------------------------------


def test_the_token_file_lives_under_xdg_config_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert token_path() == tmp_path / "felix" / "token"


def test_a_saved_token_reads_back_and_only_its_owner_can_read_it(tmp_path: Path) -> None:
    token = _token()
    path = save_token(token, tmp_path / "felix" / "token")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert load_token(_BASE, path) == token


def test_saving_over_a_world_readable_file_narrows_it(tmp_path: Path) -> None:
    path = tmp_path / "token"
    path.write_text("{}")
    path.chmod(0o644)
    save_token(_token(), path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_token_is_never_handed_to_another_server(tmp_path: Path) -> None:
    path = save_token(_token(), tmp_path / "token")
    assert load_token("http://elsewhere.test", path) is None
    assert load_token(f"{_BASE}/", path) is not None  # the same server, trailing slash and all


def test_an_expired_token_is_not_loaded(tmp_path: Path) -> None:
    path = save_token(_token(expires_at=time.time() - 1), tmp_path / "token")
    assert load_token(_BASE, path) is None


@pytest.mark.parametrize("content", ["", "not json", "[]", '{"access_token": "x"}'])
def test_an_unreadable_token_file_is_no_token(tmp_path: Path, content: str) -> None:
    path = tmp_path / "token"
    path.write_text(content)
    assert load_token(_BASE, path) is None


def test_no_token_file_is_no_token(tmp_path: Path) -> None:
    assert load_token(_BASE, tmp_path / "missing") is None


# --- the flow, without a server -----------------------------------------------------------


def _felix(polls: list[httpx.Response], *, expires_in: int = 900) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/device"):
            return httpx.Response(
                200,
                json={
                    "device_code": "d",
                    "user_code": "U",
                    "verification_uri": "https://github.com/login/device",
                    "expires_in": expires_in,
                    "interval": 5,
                },
            )
        return polls.pop(0)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _no_sleep(_: float) -> None:
    return None


async def test_a_code_that_expires_unapproved_stops_polling() -> None:
    pending = httpx.Response(428, json={"error": "authorization_pending", "message": "m", "interval": 5})
    polls = [pending] * 10
    now = [0.0]

    async def sleep(seconds: float) -> None:
        now[0] += seconds

    async with _felix(polls, expires_in=12) as client:
        with pytest.raises(LoginError) as info:
            await github_device_login(
                _BASE, on_code=lambda _: None, client=client, sleep=sleep, clock=lambda: now[0]
            )
    assert info.value.code == "expired_token"
    # 12 s at a 5 s interval: polls at 5 and 10, and the next would land past the deadline.
    assert len(polls) == 8


async def test_an_unreachable_server_is_a_login_error() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(down)) as client:
        with pytest.raises(LoginError) as info:
            await github_device_login(_BASE, on_code=lambda _: None, client=client, sleep=_no_sleep)
    assert info.value.code == "server_unreachable"


async def test_a_non_json_refusal_still_says_what_happened() -> None:
    async with _felix([httpx.Response(502, text="<html>bad gateway</html>")]) as client:
        with pytest.raises(LoginError) as info:
            await github_device_login(_BASE, on_code=lambda _: None, client=client, sleep=_no_sleep)
    assert (info.value.code, info.value.status) == ("http_502", 502)


def test_the_saved_file_carries_no_more_than_the_token_needs(tmp_path: Path) -> None:
    path = save_token(_token(), tmp_path / "token")
    assert set(json.loads(path.read_text())) == {"access_token", "tenant", "scopes", "expires_at", "base_url"}
