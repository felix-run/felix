"""GitHub signup: who an invite admits, the personal tenant it opens, and who may claim one.

GitHub is faked at the transport (`tests.github_fake`), as in `test_auth_github.py`, and a minted
token is checked with `verify_jwt` over the configured verifiers — the call the middleware makes.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
from felix.auth.github import GitHubLoginError, exchange_device_code
from felix.auth.github_signup import parse_signup_logins, personal_tenant_owner
from felix.auth.jwt import mint_token, parse_verifiers, verify_jwt
from felix.config import Settings
from joserfc import jwk

from tests.github_fake import FakeGitHub
from tests.github_fake import org as _org

_KEY = jwk.RSAKey.generate_key(2048)
_PRIVATE = _KEY.as_pem(private=True).decode()
_PUBLIC = _KEY.as_pem(private=False).decode()


def _settings(**kw: Any) -> Settings:
    base: dict[str, Any] = {
        "database_url": "memory://github-signup",
        "object_store": "memory",
        "environment": "development",
        "auth_mode": "jwt",
        "jwt_verifiers": "self:felix-self",
        "jwks_public": _PUBLIC,
        "jwks_private": _PRIVATE,
        "allowed_tenants": "acme",
        "github_client_id": "Iv1.test",
        "github_org_tenants": json.dumps({"acme": _org("acme", "acme", ["audit:read"])}),
        "github_signup": "invite",
        "github_signup_logins": "octo",
        "github_signup_scopes": "jobs:read,memory:read",
    }
    base.update(kw)
    return Settings(**base)


def _outsider(**user: Any) -> FakeGitHub:
    return FakeGitHub(memberships={}, user={"id": 4242, "login": "octo", **user})


async def _exchange(settings: Settings, fake: FakeGitHub) -> Any:
    async with fake.client() as client:
        return await exchange_device_code(settings, "dev-1", client=client)


async def _refused(settings: Settings, fake: FakeGitHub) -> GitHubLoginError:
    with pytest.raises(GitHubLoginError) as info:
        await _exchange(settings, fake)
    return info.value


def _verify(settings: Settings, token: str) -> Any:
    return verify_jwt(
        token, parse_verifiers(settings.jwt_verifiers), jwks_public=settings.jwks_public, settings=settings
    )


# --- admission --------------------------------------------------------------------------


async def test_an_invited_outsider_gets_a_personal_tenant_the_api_accepts() -> None:
    settings = _settings()
    minted = await _exchange(settings, _outsider())
    assert (minted.tenant, minted.scopes, minted.subject) == (
        "gh-4242",
        ("jobs:read", "memory:read"),
        "github:4242",
    )
    result = _verify(settings, minted.access_token)
    # In no FELIX_ALLOWED_TENANTS, and admitted anyway: it is this account's own sign-in.
    assert result.ok, result
    assert result.principal.tenant_id == "gh-4242"


async def test_an_uninvited_outsider_is_not_invited_and_named() -> None:
    exc = await _refused(_settings(github_signup_logins="someone-else"), _outsider())
    assert (exc.code, exc.status, exc.github_login) == ("not_invited", 403, "octo")


async def test_with_signup_off_an_outsider_is_still_not_a_member() -> None:
    exc = await _refused(
        _settings(github_signup="off", github_signup_logins="", github_signup_scopes=""), _outsider()
    )
    assert exc.code == "not_a_member"


async def test_an_org_member_is_never_moved_to_a_personal_tenant() -> None:
    minted = await _exchange(_settings(), FakeGitHub(user={"id": 4242, "login": "octo"}))
    assert minted.tenant == "acme"


async def test_a_login_matches_case_insensitively() -> None:
    minted = await _exchange(_settings(github_signup_logins="OCTO"), _outsider())
    assert minted.tenant == "gh-4242"


async def test_a_pinned_invite_follows_the_id_through_a_rename() -> None:
    minted = await _exchange(_settings(github_signup_logins="old-name:4242"), _outsider(login="new-name"))
    assert minted.tenant == "gh-4242"


async def test_a_pinned_invite_is_not_inherited_with_a_released_login(
    caplog: pytest.LogCaptureFixture,
) -> None:
    exc = await _refused(_settings(github_signup_logins="octo:1"), _outsider())
    assert exc.code == "not_invited"
    assert "github:1" in caplog.text


async def test_an_org_hiding_membership_still_explains_itself_to_the_uninvited() -> None:
    fake = FakeGitHub(memberships={"acme": (403, "")})
    exc = await _refused(_settings(github_signup_logins="someone-else"), fake)
    assert exc.code == "org_access_restricted"


async def test_an_org_hiding_membership_does_not_block_an_invite() -> None:
    fake = FakeGitHub(memberships={"acme": (403, "")}, user={"id": 4242, "login": "octo"})
    minted = await _exchange(_settings(), fake)
    assert minted.tenant == "gh-4242"


# --- who may claim a personal tenant ----------------------------------------------------


def _claim(settings: Settings, **claims: Any) -> Any:
    token = mint_token(
        settings,
        sub=claims.pop("sub"),
        tenant_id=claims.pop("tenant"),
        scopes=["jobs:read"],
        ttl_seconds=300,
        extra_claims=claims,
    )
    return _verify(settings, token)


def test_a_token_for_someone_elses_personal_tenant_is_refused() -> None:
    result = _claim(_settings(), sub="github:1", tenant="gh-4242", idp="github")
    assert (result.ok, result.reason) == (False, "tenant_not_allowed")


def test_a_personal_tenant_needs_a_github_sign_in_not_just_its_subject() -> None:
    result = _claim(_settings(), sub="github:4242", tenant="gh-4242")
    assert result.ok is False


def test_another_issuer_cannot_claim_a_personal_tenant() -> None:
    """Same key, same subject, same `idp`: only the issuer differs, and that is enough to refuse."""
    import time

    from joserfc import jwt

    settings = _settings(jwt_verifiers="self:other-issuer")
    now = int(time.time())
    claims = {
        "sub": "github:4242",
        "tenant_id": "gh-4242",
        "idp": "github",
        "scope": "jobs:read",
        "iss": "other-issuer",
        "iat": now,
        "exp": now + 300,
    }
    token = jwt.encode({"alg": "RS256"}, claims, _KEY)
    assert (_verify(settings, token).reason) == "tenant_not_allowed"
    # The control: the same token under acme verifies, so the refusal is the tenant's alone.
    other = jwt.encode({"alg": "RS256"}, {**claims, "tenant_id": "acme"}, _KEY)
    assert _verify(settings, other).ok


def test_with_signup_off_a_personal_tenant_is_just_an_unlisted_tenant() -> None:
    off = _settings(github_signup="off", github_signup_logins="", github_signup_scopes="")
    result = _claim(off, sub="github:4242", tenant="gh-4242", idp="github")
    assert (result.ok, result.reason) == (False, "tenant_not_allowed")


# --- the tenant id and the list ---------------------------------------------------------


@pytest.mark.parametrize(
    ("tenant", "owner"),
    [("gh-4242", 4242), ("gh-0", None), ("gh-007", None), ("gh-", None), ("gh-12a", None), ("acme", None)],
)
def test_a_personal_tenant_has_one_spelling(tenant: str, owner: int | None) -> None:
    assert personal_tenant_owner(tenant) == owner


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("octo,OCTO", "listed twice"),
        ("-octo", "not a GitHub login"),
        ("octo/x", "not a GitHub login"),
        ("octo:abc", "numeric GitHub id"),
        ("octo:0", "numeric GitHub id"),
    ],
)
def test_a_malformed_list_is_refused(raw: str, message: str) -> None:
    with pytest.raises(ValueError, match=re.escape(message)):
        parse_signup_logins(raw)


# --- boot -------------------------------------------------------------------------------


def test_an_invite_configuration_starts() -> None:
    _settings().validate_runtime()


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        pytest.param({"github_signup_logins": " , "}, "at least one login", id="empty-list"),
        pytest.param({"github_signup_scopes": ""}, "FELIX_GITHUB_SIGNUP_SCOPES must say", id="no-scopes"),
        pytest.param({"github_signup_scopes": "jobs:read,admin"}, "may not grant ['admin']", id="admin"),
        pytest.param({"github_signup_scopes": "*"}, "may not grant ['*']", id="wildcard"),
        pytest.param({"github_signup_logins": "octo:x"}, "FELIX_GITHUB_SIGNUP_LOGINS", id="bad-entry"),
        pytest.param({"github_client_id": ""}, "FELIX_GITHUB_CLIENT_ID must be set", id="login-off"),
        pytest.param({"allowed_tenants": "acme,gh-12"}, "personal tenant's id", id="collision"),
        pytest.param({"jwt_verifiers": "self:felix-self;tenant=fixed:acme"}, "pins the tenant", id="fixed"),
    ],
)
def test_a_signup_that_cannot_work_does_not_start(overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(RuntimeError, match=re.escape(message)):
        _settings(**overrides).validate_runtime()


def test_an_unknown_signup_mode_is_refused_rather_than_read_as_open() -> None:
    with pytest.raises(ValueError, match="github_signup"):
        _settings(github_signup="open")


def test_a_list_left_behind_with_signup_off_says_so(caplog: pytest.LogCaptureFixture) -> None:
    _settings(github_signup="off", github_signup_scopes="").validate_runtime()
    assert "nobody can sign up" in caplog.text
