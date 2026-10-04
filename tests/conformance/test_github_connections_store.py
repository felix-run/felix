"""One contract for each person's stored GitHub connection, run against both backends.

The properties a dict and a `SELECT` can quietly disagree on: that signing in again replaces the
person's own row and keeps when they first connected, that the same GitHub user in two tenants
is two rows neither can see, that a refresh rotates the stored refresh token, that callers racing
a refresh spend it once (the Postgres arm takes an advisory lock; the memory arm an asyncio lock),
and that a refusal leaves the row `revoked` rather than retrying a dead token.
"""

from __future__ import annotations

import asyncio
import base64
import os
from typing import Any

import pytest
from felix.auth import github, github_connections
from felix.auth.github import GitHubGrant

from tests.github_fake import FakeGitHub, app_grant

BACKENDS = ["memory", "postgres"]
parametrized = pytest.mark.parametrize("store_settings", BACKENDS, indirect=True)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    fake = FakeGitHub()
    monkeypatch.setattr(github, "github_http_client", lambda settings: fake.client())
    return fake


def _with_app(settings: Any) -> Any:
    return settings.model_copy(
        update={
            "github_client_id": "Iv23.conformance",
            "github_client_secret": "app-secret",
            "github_token_key": base64.b64encode(os.urandom(32)).decode(),
        }
    )


async def _connect(settings: Any, tenant: str = "acme", expires_in: int = 28_800, login: str = "octo") -> Any:
    grant = GitHubGrant(
        access_token="gho_a",
        refresh_token="ghr_1",
        expires_in=expires_in,
        refresh_token_expires_in=15_897_600,
    )
    return await github_connections.save_connection(
        settings, tenant, github_user_id=42, github_login=login, grant=grant, principal_subj="github:42"
    )


@parametrized
@pytest.mark.asyncio
async def test_signing_in_again_replaces_the_row_and_keeps_its_first_connection(store_settings: Any) -> None:
    settings = _with_app(store_settings)
    first = await _connect(settings, login="octo")
    again = await _connect(settings, login="octo-renamed")
    rows = await github_connections.list_connections(settings, "acme")
    assert [r["github_login"] for r in rows] == ["octo-renamed"]
    assert again["created_at"] == first["created_at"]
    assert rows[0]["status"] == "active"


@parametrized
@pytest.mark.asyncio
async def test_one_github_user_in_two_tenants_is_two_rows(store_settings: Any) -> None:
    settings = _with_app(store_settings)
    await _connect(settings, "acme")
    await _connect(settings, "globex")
    assert await github_connections.remove_connection(settings, "acme", 42) is True
    assert await github_connections.get_connection(settings, "acme", 42) is None
    assert (await github_connections.get_connection(settings, "globex", 42) or {})["status"] == "active"


@parametrized
@pytest.mark.asyncio
async def test_racing_callers_spend_the_refresh_token_once_and_store_the_rotated_one(
    store_settings: Any, fake: FakeGitHub
) -> None:
    settings = _with_app(store_settings)
    await _connect(settings, expires_in=60)
    fake.refresh_answers["ghr_1"] = app_grant("gho_b", "ghr_2")
    tokens = await asyncio.gather(*(github_connections.access_token(settings, "acme", 42) for _ in range(4)))
    assert tokens == ["gho_b"] * 4
    assert fake.refreshes == 1
    # The next refresh uses the rotated token: GitHub no longer honours ghr_1.
    fake.refresh_answers["ghr_2"] = app_grant("gho_c", "ghr_3", expires_in=60)
    await github_connections._force_expiry_for_tests(settings, "acme", 42)
    assert await github_connections.access_token(settings, "acme", 42) == "gho_c"
    assert fake.refreshes == 2


@parametrized
@pytest.mark.asyncio
async def test_a_refused_refresh_is_stored_as_revoked(store_settings: Any, fake: FakeGitHub) -> None:
    settings = _with_app(store_settings)
    await _connect(settings, expires_in=60)
    with pytest.raises(github_connections.GitHubConnectionRevoked):
        await github_connections.access_token(settings, "acme", 42)
    assert (await github_connections.get_connection(settings, "acme", 42) or {})["status"] == "revoked"
