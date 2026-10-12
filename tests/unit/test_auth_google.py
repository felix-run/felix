"""Google sign-in: the allowlist, the ID-token checks, what is minted, and what refuses to boot.

The code exchange runs against `FakeGoogle` (token endpoint + JWKS), so every refusal below is
the real `exchange_code` path reaching the check it names — not a helper called in isolation.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from typing import Any

import pytest
from felix.auth import google
from felix.auth.context import MANIFESTS_CLAIM
from felix.auth.jwt import parse_verifiers, verify_jwt
from joserfc import jwk

from tests.support.factories import make_settings
from tests.support.google_fake import CLIENT_ID, CLIENT_SECRET, KID_SUB, FakeGoogle

_SELF_KEY = jwk.RSAKey.generate_key(2048)
APP = "http://localhost:5181"
KID = {"tenant": "kids", "scopes": [], "manifests": ["kids"]}
PARENT = {"tenant": "family", "scopes": ["sessions:read"]}


def _settings(accounts: dict[str, Any] | None = None, **overrides: Any) -> Any:
    values: dict[str, Any] = {
        "auth_mode": "jwt",
        "jwt_verifiers": "self:felix-self",
        "jwks_public": _SELF_KEY.as_pem(private=False).decode(),
        "jwks_private": _SELF_KEY.as_pem(private=True).decode(),
        "allowed_tenants": "kids,family",
        "google_client_id": CLIENT_ID,
        "google_client_secret": CLIENT_SECRET,
        "google_cookie_key": base64.b64encode(os.urandom(32)).decode(),
        "google_redirect_origins": APP,
        "google_accounts": json.dumps(accounts if accounts is not None else {"kid@example.com": KID}),
    }
    values.update(overrides)
    return make_settings(**values)


@pytest.fixture(autouse=True)
def _fresh_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(google, "_keys", google.new_key_cache())


VERIFIER = "v" * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip("=")
REDIRECT_URI = f"{APP}/api/auth/google/callback"


async def _exchange(settings: Any, fake: FakeGoogle, code: str) -> Any:
    async with fake.client() as http:
        return await google.exchange_code(
            settings, code, code_verifier=VERIFIER, redirect_uri=REDIRECT_URI, nonce="n-1", client=http
        )


async def _sign_in(settings: Any, fake: FakeGoogle, *, nonce: str = "n-1", **claims: Any) -> Any:
    """Approve on Google's screen (issuing an ID token carrying `nonce`), then run the callback's
    exchange as the browser whose flow cookie holds nonce `n-1`."""
    query = {"code_challenge": CHALLENGE, "redirect_uri": REDIRECT_URI, "nonce": nonce}
    return await _exchange(settings, fake, fake.issue_code(query, **claims))


def _verified(settings: Any, token: str) -> Any:
    result = verify_jwt(
        token, parse_verifiers(settings.jwt_verifiers), jwks_public=settings.jwks_public, settings=settings
    )
    assert result.ok, result.reason
    return result


# --- the allowlist ------------------------------------------------------------------------


def test_accounts_are_keyed_by_lower_cased_email() -> None:
    accounts = google.parse_accounts(json.dumps({"Kid@Example.COM": KID}))
    assert list(accounts) == ["kid@example.com"]
    assert accounts["kid@example.com"].manifests == ("kids",)
    assert google.parse_accounts(json.dumps({"p@example.com": PARENT}))["p@example.com"].manifests is None


@pytest.mark.parametrize(
    ("raw", "fragment"),
    [
        ({"not-an-email": KID}, "not an email"),
        ({"a@example.com": KID, "A@example.com": KID}, "listed twice"),
        ({"a@example.com": {**KID, "role": "kid"}}, "unknown keys"),
        ({"a@example.com": {"scopes": []}}, "`tenant` is required"),
        ({"a@example.com": {**KID, "manifests": []}}, "`manifests` is empty"),
        ({"a@example.com": {**KID, "manifests": "kids"}}, "list of non-empty strings"),
        ({"a@example.com": {**KID, "sub": 12345}}, "numeric Google account id"),
        ({"a@example.com": {**KID, "sub": "abc"}}, "numeric Google account id"),
        # `$` matches before a final newline; a pin that can never equal a real `sub` is a typo.
        ({"a@example.com": {**KID, "sub": "123\n"}}, "numeric Google account id"),
    ],
    ids=[
        "email",
        "duplicate",
        "unknown-key",
        "tenant",
        "empty-pin",
        "pin-not-list",
        "sub-int",
        "sub-text",
        "sub-newline",
    ],
)
def test_a_malformed_account_is_refused(raw: dict[str, Any], fragment: str) -> None:
    with pytest.raises(ValueError, match=fragment):
        google.parse_accounts(json.dumps(raw))


# --- signing in ---------------------------------------------------------------------------


async def test_a_listed_account_gets_a_token_pinned_to_its_manifests() -> None:
    settings = _settings()
    minted, account = await _sign_in(settings, FakeGoogle())
    assert (minted.tenant, minted.subject, account.email) == ("kids", f"google:{KID_SUB}", "kid@example.com")
    result = _verified(settings, minted.access_token)
    assert result.principal.tenant_id == "kids"
    assert result.principal.scopes == frozenset()
    assert result.payload[MANIFESTS_CLAIM] == ["kids"]
    assert result.payload["idp"] == "google"


async def test_an_unpinned_account_carries_no_manifest_claim() -> None:
    settings = _settings({"parent@example.com": PARENT})
    minted, _ = await _sign_in(settings, FakeGoogle(), email="Parent@Example.com")
    payload = _verified(settings, minted.access_token).payload
    assert MANIFESTS_CLAIM not in payload
    assert payload["scope"] == "sessions:read"


@pytest.mark.parametrize(
    ("claims", "code"),
    [
        ({"email": "stranger@example.com"}, "not_allowed"),
        ({"iss": "https://evil.example.com"}, "invalid_id_token"),
        ({"aud": "someone-elses-client"}, "invalid_id_token"),
        ({"email_verified": False}, "invalid_id_token"),
        # Google sends a boolean; a string is not verification.
        ({"email_verified": "true"}, "invalid_id_token"),
        ({"email_verified": None}, "invalid_id_token"),
        ({"exp": 1}, "invalid_id_token"),
    ],
    ids=[
        "not-listed",
        "issuer",
        "audience",
        "unverified",
        "verified-as-string",
        "verified-absent",
        "expired",
    ],
)
async def test_a_sign_in_is_refused(claims: dict[str, Any], code: str) -> None:
    with pytest.raises(google.GoogleLoginError) as refused:
        await _sign_in(_settings(), FakeGoogle(), **claims)
    assert refused.value.code == code


async def test_an_id_token_issued_for_another_flow_is_refused() -> None:
    """The nonce in the ID token is the one Google was sent; the flow cookie holds this browser's."""
    with pytest.raises(google.GoogleLoginError) as refused:
        await _sign_in(_settings(), FakeGoogle(), nonce="someone-elses-nonce")
    assert refused.value.code == "invalid_id_token"


async def test_a_pinned_sub_refuses_another_account_with_the_same_email(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _settings({"kid@example.com": {**KID, "sub": "42"}})
    with pytest.raises(google.GoogleLoginError) as refused:
        await _sign_in(settings, FakeGoogle())
    assert refused.value.code == "not_allowed"
    # The operator's way to pin the right account: the refusal logs the id that tried.
    assert KID_SUB in caplog.text
    minted, _ = await _sign_in(_settings({"kid@example.com": {**KID, "sub": KID_SUB}}), FakeGoogle())
    assert minted.subject == f"google:{KID_SUB}"


async def test_a_replayed_code_is_refused() -> None:
    fake, settings = FakeGoogle(), _settings()
    code = fake.issue_code({"code_challenge": CHALLENGE, "redirect_uri": REDIRECT_URI, "nonce": "n-1"})
    await _exchange(settings, fake, code)
    with pytest.raises(google.GoogleLoginError) as refused:
        await _exchange(settings, fake, code)
    assert refused.value.code == "invalid_id_token"


def test_authorize_url_asks_for_openid_email_with_pkce_and_a_nonce() -> None:
    from urllib.parse import parse_qs, urlsplit

    url = google.authorize_url(
        _settings(), redirect_uri=f"{APP}/api/auth/google/callback", state="s", nonce="n", code_challenge="c"
    )
    query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    assert url.startswith(google.GOOGLE_AUTHORIZE_URL)
    assert query["scope"] == "openid email"
    assert (query["nonce"], query["state"], query["code_challenge_method"]) == ("n", "s", "S256")
    assert query["client_id"] == CLIENT_ID


# --- boot ---------------------------------------------------------------------------------


def test_a_good_config_boots() -> None:
    google.validate_google_config(_settings({"kid@example.com": KID, "parent@example.com": PARENT}))


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"google_accounts": ""}, "must list at least one account"),
        ({"google_accounts": "[]"}, "FELIX_GOOGLE_ACCOUNTS"),
        ({"google_cookie_key": "not base64!"}, "FELIX_GOOGLE_COOKIE_KEY"),
        ({"google_redirect_origins": "https://app.example.com/path"}, "is not an origin"),
        ({"google_redirect_origins": "http://app.example.com"}, "plain http"),
        ({"google_callback_path": "api/cb"}, "must start with '/'"),
        ({"jwt_verifiers": "", "auth_mode": "api_key"}, "FELIX_AUTH_MODE=jwt"),
        ({"allowed_tenants": "family"}, "not in FELIX_ALLOWED_TENANTS"),
    ],
    ids=[
        "no-accounts",
        "not-object",
        "cookie-key",
        "origin-path",
        "origin-http",
        "callback",
        "no-jwt",
        "tenant",
    ],
)
def test_a_config_that_would_fail_on_first_sign_in_refuses_to_boot(
    overrides: dict[str, Any], fragment: str
) -> None:
    settings = _settings()
    for name, value in overrides.items():
        object.__setattr__(settings, name, value)
    with pytest.raises(RuntimeError, match=fragment):
        google.validate_google_config(settings)


@pytest.mark.parametrize(
    "scope", ["admin", "*", "manifests:write", "jobs:write", "eval:write", ":read", "a:b:read"]
)
def test_a_pinned_account_may_carry_only_read_scopes(scope: str) -> None:
    """A write scope reaches around a pin: rewrite the pinned manifest, or run any as `cron`."""
    settings = _settings({"kid@example.com": {**KID, "scopes": ["sessions:read", scope]}})
    with pytest.raises(RuntimeError, match="may carry only"):
        google.validate_google_config(settings)


def test_a_pinned_account_with_read_scopes_boots() -> None:
    google.validate_google_config(_settings({"kid@example.com": {**KID, "scopes": ["sessions:read"]}}))


def test_a_pinned_account_may_not_share_a_tenant_with_an_unpinned_one() -> None:
    """Threads are shared across a tenant, so the child could read the parent's."""
    settings = _settings({"kid@example.com": KID, "parent@example.com": {**PARENT, "tenant": "kids"}})
    with pytest.raises(RuntimeError, match="shares tenant 'kids'"):
        google.validate_google_config(settings)


def test_accounts_without_a_client_id_only_warn(caplog: pytest.LogCaptureFixture) -> None:
    settings = _settings()
    object.__setattr__(settings, "google_client_id", "")
    google.validate_google_config(settings)
    assert "Google sign-in is off" in caplog.text


def test_sign_in_paths_are_public_only_while_configured() -> None:
    assert google.public_login_paths(_settings()) == google.GOOGLE_REDIRECT_PATHS
    off = _settings()
    object.__setattr__(off, "google_client_secret", "")
    assert google.public_login_paths(off) == frozenset()
