"""`felix_client.login` without a server: the saved token file, the URL, the cadence, the network.

The happy path and every server refusal run against the real routes in
`tests/e2e/test_github_login_client.py`. What is here is what no honest server answer reaches.
"""

from __future__ import annotations

import json
import stat
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from felix_client import FelixClient
from felix_client.login import (
    TOKEN_FILE_VERSION,
    DeviceCode,
    LoginError,
    LoginToken,
    TokenFileError,
    bearer_for,
    check_url,
    github_device_login,
    load_token,
    save_token,
    token_path,
)

_BASE = "https://felix.test"


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


@pytest.fixture
def token_file(tmp_path: Path) -> Path:
    return tmp_path / "felix" / "token"


# --- where it lives -----------------------------------------------------------------------


def test_the_token_file_lives_under_xdg_config_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert token_path() == tmp_path / "felix" / "token"


@pytest.mark.parametrize("xdg", [None, ""])
def test_without_xdg_config_home_it_is_under_dot_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, xdg: str | None
) -> None:
    if xdg is None:
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    else:
        monkeypatch.setenv("XDG_CONFIG_HOME", xdg)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert token_path() == tmp_path / ".config" / "felix" / "token"


# --- saving and loading -------------------------------------------------------------------


def test_a_saved_token_reads_back_and_only_its_owner_can_read_it(token_file: Path) -> None:
    token = _token()
    save_token(token, token_file)
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(token_file.parent.stat().st_mode) == 0o700
    assert load_token(_BASE, token_file) == token


def test_the_file_is_versioned_and_keyed_by_server(token_file: Path) -> None:
    save_token(_token(), token_file)
    data = json.loads(token_file.read_text())
    assert data["version"] == TOKEN_FILE_VERSION
    assert list(data["tokens"]) == [_BASE]
    assert set(data["tokens"][_BASE]) == {"access_token", "tenant", "scopes", "expires_at", "base_url"}


def test_logging_in_to_a_second_server_keeps_the_first(token_file: Path) -> None:
    save_token(_token(), token_file)
    save_token(_token(base_url="https://other.test", tenant="globex"), token_file)
    assert load_token(_BASE, token_file).tenant == "acme"  # type: ignore[union-attr]
    assert load_token("https://other.test", token_file).tenant == "globex"  # type: ignore[union-attr]


def test_saving_drops_other_servers_expired_tokens(token_file: Path) -> None:
    save_token(_token(base_url="https://stale.test", expires_at=time.time() - 1), token_file)
    save_token(_token(), token_file)
    assert list(json.loads(token_file.read_text())["tokens"]) == [_BASE]


def test_a_token_is_never_handed_to_another_server(token_file: Path) -> None:
    save_token(_token(), token_file)
    assert load_token("https://elsewhere.test", token_file) is None
    assert load_token(f"{_BASE}/", token_file) == load_token(_BASE, token_file)


def test_an_expired_token_is_not_loaded(token_file: Path) -> None:
    save_token(_token(expires_at=time.time() - 1), token_file)
    assert load_token(_BASE, token_file) is None


@pytest.mark.parametrize(
    "content",
    [
        "",
        "not json",
        "[]",
        '{"version": 2, "tokens": {}}',
        '{"version": 1, "tokens": {"https://felix.test": {}}}',
    ],
)
def test_an_unreadable_token_file_is_no_token(token_file: Path, content: str) -> None:
    token_file.parent.mkdir(mode=0o700)
    token_file.write_text(content)
    token_file.chmod(0o600)
    assert load_token(_BASE, token_file) is None


def test_no_token_file_is_no_token(token_file: Path) -> None:
    assert load_token(_BASE, token_file) is None


# --- a file that is not plainly this user's -----------------------------------------------


def test_overwriting_a_world_readable_file_never_writes_into_it(token_file: Path) -> None:
    """The token goes into a fresh 0600 file that replaces the old one, not into the old inode."""
    token_file.parent.mkdir(mode=0o700)
    token_file.write_text("{}")
    token_file.chmod(0o644)
    old_inode = token_file.stat().st_ino
    save_token(_token(), token_file)
    assert token_file.stat().st_ino != old_inode
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600


def test_a_symlink_at_the_token_path_is_replaced_not_followed(token_file: Path, tmp_path: Path) -> None:
    bait = tmp_path / "attacker-readable"
    bait.write_text("")
    token_file.parent.mkdir(mode=0o700)
    token_file.symlink_to(bait)
    save_token(_token(), token_file)
    assert bait.read_text() == ""
    assert not token_file.is_symlink()


def test_a_shared_token_directory_is_refused(token_file: Path) -> None:
    token_file.parent.mkdir(mode=0o700)
    token_file.parent.chmod(0o777)
    with pytest.raises(TokenFileError):
        save_token(_token(), token_file)
    assert not token_file.exists()


def test_a_token_file_others_can_write_is_not_trusted(token_file: Path) -> None:
    """A planted token for someone else's tenant would route this user's work there."""
    save_token(_token(), token_file)
    token_file.chmod(0o666)
    assert load_token(_BASE, token_file) is None


def test_a_symlinked_token_file_is_not_trusted(token_file: Path, tmp_path: Path) -> None:
    real = tmp_path / "elsewhere" / "token"
    save_token(_token(), real)
    token_file.parent.mkdir(mode=0o700)
    token_file.symlink_to(real)
    assert load_token(_BASE, token_file) is None


def test_the_token_is_not_in_its_repr() -> None:
    assert "a.b.c" not in repr(_token())


# --- which bearer a client sends ----------------------------------------------------------


def test_an_explicit_bearer_wins_over_a_saved_login(token_file: Path) -> None:
    save_token(_token(), token_file)
    assert bearer_for(_BASE, "explicit", token_file) == "explicit"
    assert bearer_for(_BASE, None, token_file) == "a.b.c"
    assert bearer_for("https://elsewhere.test", None, token_file) is None


def test_felix_client_from_login_carries_the_saved_bearer(token_file: Path) -> None:
    save_token(_token(), token_file)
    client = FelixClient.from_login(_BASE, token_file=token_file)
    assert client._headers()["authorization"] == "Bearer a.b.c"
    assert "x-felix-tenant" not in client._headers()
    assert FelixClient.from_login("https://elsewhere.test", token_file=token_file).api_key is None


# --- the URL ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("https://felix.example", True),
        ("http://localhost:8080", True),
        ("http://127.0.0.1:8080/", True),
        ("http://[::1]:8080", True),
        ("http://felix.example", False),
        ("http://localhost.evil.example", False),
    ],
)
def test_plain_http_is_for_loopback(url: str, allowed: bool) -> None:
    if allowed:
        assert check_url(url) == url.rstrip("/")
    else:
        with pytest.raises(LoginError) as info:
            check_url(url)
        assert info.value.code == "insecure_url"
        assert check_url(url, allow_insecure=True) == url


# --- the flow, against a fake server ------------------------------------------------------


def _felix(
    polls: list[httpx.Response], *, device: dict[str, Any] | None = None, device_text: str | None = None
) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/device"):
            if device_text is not None:
                return httpx.Response(200, text=device_text)
            body = {
                "device_code": "d",
                "user_code": "U",
                "verification_uri": "https://github.com/login/device",
                "expires_in": 900,
                "interval": 5,
                **(device or {}),
            }
            return httpx.Response(200, json=body)
        return polls.pop(0)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class Clock:
    """`sleep` and `clock` that move together, so a deadline can pass without waiting for it."""

    def __init__(self) -> None:
        self.now = 0.0
        self.waited: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.waited.append(seconds)
        self.now += seconds

    def __call__(self) -> float:
        return self.now


async def _run(client: httpx.AsyncClient, clock: Clock | None = None, **kwargs: Any) -> LoginToken:
    clock = clock or Clock()
    kwargs.setdefault("on_code", lambda _: None)
    return await github_device_login(_BASE, client=client, sleep=clock.sleep, clock=clock, **kwargs)


def _pending() -> httpx.Response:
    return httpx.Response(428, json={"error": "authorization_pending", "message": "m", "interval": 5})


def _granted() -> httpx.Response:
    body = {"access_token": "t", "token_type": "Bearer", "expires_in": 60, "tenant": "acme", "scopes": []}
    return httpx.Response(200, json=body)


async def test_a_code_that_expires_unapproved_stops_polling() -> None:
    polls = [_pending() for _ in range(10)]
    async with _felix(polls, device={"expires_in": 12}) as client:
        with pytest.raises(LoginError) as info:
            await _run(client)
    assert info.value.code == "expired_token"
    # 12 s at a 5 s interval: polls at 5 and 10, and the next would land past the deadline.
    assert len(polls) == 8


async def test_a_deadline_shorter_than_the_interval_never_sleeps() -> None:
    clock = Clock()
    async with _felix([], device={"expires_in": 3}) as client:
        with pytest.raises(LoginError):
            await _run(client, clock)
    assert clock.waited == []


@pytest.mark.parametrize(("interval", "slept"), [(0, 1), (-5, 1), (10**9, 60)])
async def test_the_server_chosen_interval_is_bounded(interval: int, slept: int) -> None:
    clock = Clock()
    async with _felix([_granted()], device={"interval": interval}) as client:
        await _run(client, clock)
    assert clock.waited == [slept]


async def test_a_huge_expiry_is_capped() -> None:
    clock = Clock()
    polls = [_pending() for _ in range(400)]
    async with _felix(polls, device={"expires_in": 10**9, "interval": 60}) as client:
        with pytest.raises(LoginError):
            await _run(client, clock)
    assert clock.now <= 1800


async def test_server_text_is_shown_without_control_characters() -> None:
    shown: list[DeviceCode] = []
    device = {"user_code": "AB\x1b[2JCD", "verification_uri": "https://github.com/\x07x"}
    async with _felix([_granted()], device=device) as client:
        await _run(client, on_code=shown.append)
    assert (shown[0].user_code, shown[0].verification_uri) == ("AB[2JCD", "https://github.com/x")


async def test_an_async_on_code_is_awaited() -> None:
    shown: list[str] = []

    async def show(code: DeviceCode) -> None:
        shown.append(code.user_code)

    async with _felix([_granted()]) as client:
        await _run(client, on_code=show)
    assert shown == ["U"]


@pytest.mark.parametrize(
    "fake",
    [
        pytest.param({"device_text": "<html>captive portal</html>"}, id="html-start"),
        pytest.param({"device": {"user_code": None, "interval": "soon"}}, id="bad-start-fields"),
    ],
)
async def test_a_malformed_start_is_a_login_error(fake: dict[str, Any]) -> None:
    async with _felix([], **fake) as client:
        with pytest.raises(LoginError) as info:
            await _run(client)
    assert info.value.code == "bad_response"


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(httpx.Response(200, text="<html>"), id="html-token"),
        pytest.param(httpx.Response(200, json={"access_token": "t"}), id="incomplete-token"),
    ],
)
async def test_a_malformed_token_answer_is_a_login_error(answer: httpx.Response) -> None:
    async with _felix([answer]) as client:
        with pytest.raises(LoginError) as info:
            await _run(client)
    assert info.value.code == "bad_response"


async def test_an_unreachable_server_is_a_login_error() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(down)) as client:
        with pytest.raises(LoginError) as info:
            await _run(client)
    assert info.value.code == "server_unreachable"


async def test_a_non_json_refusal_still_says_what_happened() -> None:
    async with _felix([httpx.Response(502, text="<html>bad gateway</html>")]) as client:
        with pytest.raises(LoginError) as info:
            await _run(client)
    assert (info.value.code, info.value.status) == ("http_502", 502)


async def test_an_insecure_url_is_refused_before_any_request() -> None:
    requests: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(record)) as client:
        with pytest.raises(LoginError) as info:
            await github_device_login("http://felix.example", client=client)
    assert info.value.code == "insecure_url"
    assert requests == []
