"""Google sign-in: the allowlist, the ID-token checks, what is minted, and what refuses to boot.

The code exchange runs against `FakeGoogle` (token endpoint + JWKS), so every refusal below is
the real `exchange_code` path reaching the check it names — not a helper called in isolation.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
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
TRIGGER = {
    "tenant": "kids",
    "manifest": "kids",
    "scheme": "standard",
    "secret": "secret:HOOK_KEY",
    "prompt": "An event arrived.",
}


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
        # Joined with spaces into the token's `scope` claim and split on whitespace when verified:
        # this would pass the pinned-account check as one read scope and arrive as `admin`.
        ({"a@example.com": {**KID, "scopes": ["admin memory:read"]}}, "may not contain whitespace"),
        ({"a@example.com": {**KID, "scopes": ["memory:read\tadmin"]}}, "may not contain whitespace"),
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
        "scope-space",
        "scope-tab",
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


def test_authorize_url_asks_for_openid_email_profile_with_pkce_and_a_nonce() -> None:
    from urllib.parse import parse_qs, urlsplit

    url = google.authorize_url(
        _settings(), redirect_uri=f"{APP}/api/auth/google/callback", state="s", nonce="n", code_challenge="c"
    )
    query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    assert url.startswith(google.GOOGLE_AUTHORIZE_URL)
    assert query["scope"] == "openid email profile"
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


@pytest.mark.parametrize(
    ("accounts", "overrides", "fragment"),
    [
        (
            {"kid@example.com": KID, "sib@example.com": {**KID, "manifests": ["kids", "deep"]}},
            {},
            "an account pinned to different ones",
        ),
        (
            {"kid@example.com": {**KID, "tenant": "default"}},
            {"allowed_tenants": "default"},
            "may not be in tenant 'default', where every API key without a `tenant_id` lands",
        ),
        (
            {"kid@example.com": KID},
            {"auth_api_keys": json.dumps({"k-1": {"tenant_id": "kids", "scopes": []}})},
            "shares tenant 'kids' with FELIX_AUTH_API_KEYS (kids)",
        ),
        (
            {"kid@example.com": KID},
            {"github_org_tenants": json.dumps({"acme": {"id": 1, "tenant": "kids", "scopes": []}})},
            "shares tenant 'kids' with FELIX_GITHUB_ORG_TENANTS (acme)",
        ),
        (
            {"kid@example.com": KID},
            {"jwt_verifiers": "self:felix-self,cognito:https://idp.example.com;tenant=fixed:kids"},
            "shares tenant 'kids' with FELIX_JWT_VERIFIERS",
        ),
        (
            {"kid@example.com": KID},
            {"triggers": json.dumps({"nightly": TRIGGER})},
            "shares tenant 'kids' with FELIX_TRIGGERS (nightly)",
        ),
    ],
    ids=["two-pins", "default-tenant", "api-key", "github-org", "fixed-jwt", "trigger"],
)
def test_a_pinned_account_must_have_its_tenant_to_itself(
    accounts: dict[str, Any], overrides: dict[str, Any], fragment: str
) -> None:
    """Every other door into the tenant reaches threads the pin was meant to keep out of reach."""
    settings = _settings(accounts)
    for name, value in overrides.items():
        object.__setattr__(settings, name, value)
    with pytest.raises(RuntimeError, match=re.escape(fragment)):
        google.validate_google_config(settings)


def test_an_api_key_without_a_tenant_is_in_default() -> None:
    """`AuthMiddleware` puts a key with no `tenant_id` in `default`; the boot checks read the same
    rule (`felix.auth.api_keys.parse_api_keys`), so a pinned account can never be in a tenant such a key reaches."""
    from felix.config import configured_tenants

    settings = _settings({"kid@example.com": KID})
    object.__setattr__(settings, "auth_api_keys", json.dumps({"k-1": {"scopes": []}}))
    google.validate_google_config(settings)  # `kids` is not `default`
    keys = [(t.label, t.tenant) for t in configured_tenants(settings) if t.kind == "api_key"]
    assert keys == [("FELIX_AUTH_API_KEYS (default)", "default")]


@pytest.mark.parametrize(
    "verifiers",
    [
        # The self verifier's own fixed tenant: the login probe requires it to be the account's.
        "self:felix-self;tenant=fixed:kids",
        # A claim-mode issuer's tenants cannot be known at boot.
        "self:felix-self,cognito:https://idp.example.com",
    ],
    ids=["self-fixed", "claim-mode"],
)
def test_a_pinned_account_boots_beside_verifiers_that_name_no_other_tenant(verifiers: str) -> None:
    settings = _settings({"kid@example.com": KID})
    object.__setattr__(settings, "jwt_verifiers", verifiers)
    google.validate_google_config(settings)


def test_every_tenant_source_is_counted_or_exempt_for_a_pin() -> None:
    """A tenant source added to `configured_tenants` must be decided for pinned-account isolation:
    counted (a door into the tenant the pin does not hold) or exempt with a reason."""
    from typing import get_args

    from felix.config import TenantSourceKind

    kinds = set(get_args(TenantSourceKind))
    assert not (google.PIN_ISOLATION_COUNTED & set(google.PIN_ISOLATION_EXEMPT))
    assert google.PIN_ISOLATION_COUNTED | set(google.PIN_ISOLATION_EXEMPT) == kinds
    assert all(reason.strip() for reason in google.PIN_ISOLATION_EXEMPT.values())


def test_configured_tenants_reports_every_kind() -> None:
    """Each kind is emitted by some setting, so the drift test above is about real sources."""
    from typing import get_args

    from felix.config import TenantSourceKind, configured_tenants

    settings = _settings({"kid@example.com": KID})
    for name, value in {
        "jwt_verifiers": "self:felix-self;tenant=fixed:kids,cognito:https://idp.example.com;tenant=fixed:ops",
        "auth_api_keys": json.dumps({"k-1": {"tenant_id": "ops"}}),
        "github_org_tenants": json.dumps({"acme": {"id": 1, "tenant": "ops", "scopes": []}}),
        "skill_update_webhooks": "ops=ops-hook",
        "triggers": json.dumps({"nightly": {**TRIGGER, "tenant": "ops"}}),
    }.items():
        object.__setattr__(settings, name, value)
    assert {t.kind for t in configured_tenants(settings)} == set(get_args(TenantSourceKind))


def test_pinned_accounts_with_the_same_pin_may_share_a_tenant() -> None:
    google.validate_google_config(
        _settings({"kid@example.com": KID, "sib@example.com": {**KID, "manifests": ["kids"]}})
    )


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


# --- the profile a client shows -------------------------------------------------------------

PIC = "https://lh3.googleusercontent.com/a/ACg8ocK=s96-c"


@pytest.mark.parametrize(
    ("claims", "expected"),
    [
        ({"name": "Jade Bauman", "picture": PIC}, ("Jade Bauman", PIC)),
        ({}, ("", "")),
        ({"name": 7, "picture": None}, ("", "")),
        ({"name": "Jade‮\x00\nBauman"}, ("JadeBauman", "")),
        ({"name": "x" * 500}, ("x" * 120, "")),
        ({"picture": "http://lh3.googleusercontent.com/a/x"}, ("", "")),
        ({"picture": "https://evil.example.com/a.googleusercontent.com"}, ("", "")),
        ({"picture": "https://lh3.googleusercontent.com@evil.example.com/a"}, ("", "")),
        ({"picture": "https://user@lh3.googleusercontent.com/a"}, ("", "")),
        ({"picture": "javascript:alert(1)//.googleusercontent.com"}, ("", "")),
        ({"picture": "https://lh3.googleusercontent.com/" + "a" * 3000}, ("", "")),
    ],
    ids=[
        "both",
        "absent",
        "wrong-types",
        "control-chars",
        "long-name",
        "http",
        "other-host",
        "userinfo-host",
        "userinfo",
        "scheme",
        "long-url",
    ],
)
def test_the_profile_is_held_to_what_a_client_may_show(
    claims: dict[str, Any], expected: tuple[str, str]
) -> None:
    assert google.profile_from(claims) == expected


def test_sign_in_asks_google_for_the_profile() -> None:
    from urllib.parse import parse_qs, urlsplit

    url = google.authorize_url(
        _settings(), redirect_uri=REDIRECT_URI, state="s", nonce="n", code_challenge="c"
    )
    assert parse_qs(urlsplit(url).query)["scope"] == ["openid email profile"]


async def test_the_profile_reaches_the_client_and_never_the_token() -> None:
    settings = _settings()
    minted, _ = await _sign_in(settings, FakeGoogle(), name="Jade Bauman", picture=PIC)
    assert (minted.email, minted.display_name, minted.avatar_url) == ("kid@example.com", "Jade Bauman", PIC)
    payload = _verified(settings, minted.access_token).payload
    assert "name" not in payload and "picture" not in payload


async def test_the_client_secret_and_cookie_key_can_come_from_the_secrets_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production reads credentials from Secret Manager. Left out of the hydrate map, these two
    had to sit in the VM's .env as plaintext, and neither reached the redaction sinks."""
    from felix import secrets

    cookie = base64.b64encode(os.urandom(32)).decode()
    stored = {"GOOGLE_CLIENT_SECRET": "GOCSPX-from-the-backend", "GOOGLE_COOKIE_KEY": cookie}

    class Backend:
        async def get(self, name: str) -> str | None:
            return stored.get(name)

    monkeypatch.setattr(secrets, "build_secrets", lambda settings: Backend())
    settings = _settings(google_client_secret="", google_cookie_key="")
    assert not google.redirect_enabled(settings)
    await secrets.hydrate_secrets(settings)
    assert (settings.google_client_secret, settings.google_cookie_key) == ("GOCSPX-from-the-backend", cookie)
    assert google.redirect_enabled(settings)
    assert "GOCSPX-from-the-backend" in secrets.collected_secret_values(settings)
