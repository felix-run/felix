"""Each person's stored GitHub connection (`felix.auth.github_connections`), on `memory://`.

The store's promises, each one a way a refresh-token store goes wrong: tokens are sealed and
bound to their row and column, a grant with nothing to refresh is not kept, a refresh happens
only near expiry and rotates, two callers at once spend the refresh token once, a refusal revokes
for good, and removing a connection withdraws it at GitHub as well.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from typing import Any

import pytest
from felix.auth import github, github_connections
from felix.auth.github import GitHubGrant
from felix.auth.github_connections import (
    GitHubConnectionRevoked,
    GitHubNotConnected,
    SealError,
    access_token,
    get_connection,
    remove_connection,
    save_connection,
    seal,
    unseal,
)
from felix.config import Settings

from tests.support.github_fake import FakeGitHub, app_grant

KEY = base64.b64encode(os.urandom(32)).decode()


def _settings(**over: Any) -> Settings:
    values: dict[str, Any] = {
        "database_url": "memory://connections",
        "github_client_id": "Iv23.app",
        "github_client_secret": "app-secret",
        "github_token_key": KEY,
    }
    values.update(over)
    return Settings(**values)


@pytest.fixture(autouse=True)
def _clean() -> Any:
    github_connections.reset_github_connections_for_tests()
    yield
    github_connections.reset_github_connections_for_tests()


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    fake = FakeGitHub()
    monkeypatch.setattr(github, "github_http_client", lambda settings: fake.client())
    return fake


def _grant(access: str = "gho_a", refresh: str = "ghr_1", expires_in: int = 28_800) -> GitHubGrant:
    return GitHubGrant(
        access_token=access, refresh_token=refresh, expires_in=expires_in, refresh_token_expires_in=15_897_600
    )


async def _connect(settings: Settings, **grant: Any) -> None:
    await save_connection(settings, "acme", github_user_id=42, github_login="octo", grant=_grant(**grant))


def test_a_sealed_value_opens_only_for_its_purpose_and_key() -> None:
    settings = _settings()
    sealed = seal(settings, "github-connection:acme:42:refresh", b"ghr_secret")
    assert b"ghr_secret" not in sealed.encode()
    assert unseal(settings, "github-connection:acme:42:refresh", sealed) == b"ghr_secret"
    # Another row, another column, another key: none of them opens it.
    for purpose in ("github-connection:acme:43:refresh", "github-connection:acme:42:access"):
        with pytest.raises(SealError):
            unseal(settings, purpose, sealed)
    with pytest.raises(SealError):
        unseal(_settings(github_token_key=base64.b64encode(os.urandom(32)).decode()), "x", sealed)


@pytest.mark.parametrize("raw", ["", "not base64!", base64.b64encode(b"short").decode()])
def test_a_token_key_that_is_not_32_bytes_is_refused(raw: str) -> None:
    with pytest.raises(ValueError):
        github_connections.token_key(_settings(github_token_key=raw))


async def test_stored_tokens_are_never_in_the_clear() -> None:
    await _connect(_settings())
    stored = json.dumps(github_connections._memory[("acme", 42)])
    assert "gho_a" not in stored and "ghr_1" not in stored
    public = await get_connection(_settings(), "acme", 42)
    assert public is not None
    assert set(public) == {
        "tenant_id",
        "github_user_id",
        "github_login",
        "status",
        "created_at",
        "updated_at",
        "refresh_expires_at",
    }


async def test_a_grant_with_nothing_to_refresh_is_not_kept() -> None:
    """An OAuth app's token never expires: keeping it would outlive any control Felix has."""
    settings = _settings()
    kept = await save_connection(
        settings,
        "acme",
        github_user_id=42,
        github_login="octo",
        grant=GitHubGrant(access_token="gho_forever"),
    )
    assert kept is None
    assert await get_connection(settings, "acme", 42) is None
    # Nor without a key to seal with.
    no_key = _settings(github_token_key="")
    assert (
        await save_connection(no_key, "acme", github_user_id=42, github_login="octo", grant=_grant()) is None
    )


async def test_a_fresh_access_token_is_used_as_is(fake: FakeGitHub) -> None:
    settings = _settings()
    await _connect(settings)
    assert await access_token(settings, "acme", 42) == "gho_a"
    assert fake.refreshes == 0


async def test_a_token_near_expiry_is_refreshed_and_the_refresh_token_rotates(fake: FakeGitHub) -> None:
    settings = _settings()
    await _connect(settings, expires_in=60)  # inside the five-minute margin
    fake.refresh_answers["ghr_1"] = app_grant("gho_b", "ghr_2")
    assert await access_token(settings, "acme", 42) == "gho_b"
    assert fake.refreshes == 1
    # The rotated refresh token is the one stored: GitHub would refuse ghr_1 now.
    row = github_connections._memory[("acme", 42)]
    assert unseal(settings, "github-connection:acme:42:refresh", row["refresh_token_sealed"]) == b"ghr_2"
    # And the new access token is reused rather than refreshed again.
    assert await access_token(settings, "acme", 42) == "gho_b"
    assert fake.refreshes == 1


async def test_two_callers_at_once_spend_the_refresh_token_once(fake: FakeGitHub) -> None:
    settings = _settings()
    await _connect(settings, expires_in=60)
    fake.refresh_answers["ghr_1"] = app_grant("gho_b", "ghr_2")
    tokens = await asyncio.gather(*(access_token(settings, "acme", 42) for _ in range(5)))
    assert tokens == ["gho_b"] * 5
    assert fake.refreshes == 1


async def test_a_refused_refresh_revokes_the_connection_for_good(fake: FakeGitHub) -> None:
    settings = _settings()
    await _connect(settings, expires_in=60)
    # No answer for ghr_1: GitHub says bad_refresh_token.
    with pytest.raises(GitHubConnectionRevoked):
        await access_token(settings, "acme", 42)
    assert (await get_connection(settings, "acme", 42) or {})["status"] == "revoked"
    fake.refresh_answers["ghr_1"] = app_grant("gho_b", "ghr_2")
    with pytest.raises(GitHubConnectionRevoked):
        await access_token(settings, "acme", 42)
    assert fake.refreshes == 0


async def test_github_unreachable_revokes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    monkeypatch.setattr(
        github, "github_http_client", lambda s: httpx.AsyncClient(transport=httpx.MockTransport(broken))
    )
    settings = _settings()
    await _connect(settings, expires_in=60)
    with pytest.raises(github.GitHubLoginError):
        await access_token(settings, "acme", 42)
    assert (await get_connection(settings, "acme", 42) or {})["status"] == "active"


async def test_a_person_without_a_connection_is_told_so() -> None:
    with pytest.raises(GitHubNotConnected):
        await access_token(_settings(), "acme", 42)


async def test_connections_are_per_tenant(fake: FakeGitHub) -> None:
    settings = _settings()
    await _connect(settings)
    with pytest.raises(GitHubNotConnected):
        await access_token(settings, "globex", 42)


async def test_removing_a_connection_forgets_it_and_withdraws_it_at_github(fake: FakeGitHub) -> None:
    settings = _settings()
    await _connect(settings)
    fake.access_tokens.add("gho_a")
    assert await remove_connection(settings, "acme", 42) is True
    assert await get_connection(settings, "acme", 42) is None
    assert fake.revoked_grants == ["gho_a"]
    assert await remove_connection(settings, "acme", 42) is False


@pytest.mark.parametrize(
    ("origins", "message"),
    [
        ("https://chat.example.com/app", "is not an origin"),
        ("ftp://chat.example.com", "is not an origin"),
        ("http://chat.example.com", "plain http"),
    ],
)
def test_a_bad_redirect_origin_fails_at_boot(origins: str, message: str) -> None:
    with pytest.raises(RuntimeError, match=message):
        github._validate_redirect_config(_settings(github_redirect_origins=origins))


def test_localhost_may_be_plain_http() -> None:
    github._validate_redirect_config(
        _settings(github_redirect_origins="http://localhost:5181,https://chat.example.com")
    )
