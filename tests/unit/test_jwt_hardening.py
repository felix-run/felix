"""JWT verification and tenant resolution.

Four holes: `exp` was not required, so a token minted without one was accepted forever;
`aud` was optional even for shared issuers that sign for every app under them; remote
JWKS was never fetched, and the fallback returned the *local self-signing key* for
Cloudflare Access / Cognito tokens; and the tenant — the isolation boundary — came from
an unvalidated claim, with a missing claim silently collapsing every such user into one
shared tenant.
"""

from __future__ import annotations

import json
import time

import pytest
from felix.auth.jwt import (
    TenantResolutionError,
    _tenant_from_payload,
    cached_jwks,
    parse_verifiers,
    verify_jwt,
)
from felix.config import Settings
from joserfc import jwk, jwt

_KEY = jwk.RSAKey.generate_key(2048)
_PUB = json.dumps({"keys": [_KEY.as_dict(private=False)]})
_ISS = "https://felix.local"


def _settings(**kw: object) -> Settings:
    base: dict[str, object] = {
        "database_url": "memory://jwt",
        "object_store": "memory",
        "allow_insecure": True,
        "auth_mode": "none",
    }
    base.update(kw)
    return Settings(**base)  # type: ignore[arg-type]


def _mint(**claims: object) -> str:
    payload = {"iss": _ISS, "sub": "user-1", **claims}
    return jwt.encode({"alg": "RS256"}, payload, _KEY)


def _verify(token: str, spec: str = f"self:{_ISS}", settings: Settings | None = None):
    return verify_jwt(token, parse_verifiers(spec), jwks_public=_PUB, settings=settings)


# --- exp ------------------------------------------------------------------------


def test_token_without_exp_is_rejected() -> None:
    """joserfc validates exp only when present, so this was accepted forever."""
    result = _verify(_mint(tenant_id="t1"))
    assert getattr(result, "principal", None) is None


def test_valid_token_is_accepted() -> None:
    result = _verify(_mint(tenant_id="t1", exp=int(time.time()) + 600))
    assert result.principal.tenant_id == "t1"  # type: ignore[union-attr]


def test_expired_token_is_reported_as_expired() -> None:
    result = _verify(_mint(tenant_id="t1", exp=int(time.time()) - 10))
    assert result.reason == "expired"  # type: ignore[union-attr]


def test_signature_failure_is_not_misreported_as_expiry() -> None:
    """`"exp" in msg` matched "unexpected", so signature errors read as expiry."""
    other = jwk.RSAKey.generate_key(2048)
    forged = jwt.encode({"alg": "RS256"}, {"iss": _ISS, "sub": "u", "exp": int(time.time()) + 600}, other)
    result = _verify(forged)
    assert result.reason != "expired"  # type: ignore[union-attr]


# --- audience on shared issuers --------------------------------------------------


def test_shared_issuer_without_audience_is_refused() -> None:
    """A Cloudflare Access issuer signs for every app in the org; with no audience
    check a token for any other app would be accepted."""
    token = _mint(exp=int(time.time()) + 600)
    result = verify_jwt(token, parse_verifiers("access:example.cloudflareaccess.com"), jwks_public=_PUB)
    assert getattr(result, "principal", None) is None


# --- remote JWKS -----------------------------------------------------------------


def test_local_key_is_not_used_for_remote_issuers() -> None:
    """The fallback returned FELIX_JWKS_PUBLIC regardless of URL, so the local
    self-signing key verified tokens claiming to come from Access/Cognito."""
    from felix.auth.jwt import _load_key_set

    assert _load_key_set(_PUB, "https://x/cdn-cgi/access/certs", "access") is None
    assert _load_key_set(_PUB, f"{_ISS}/.well-known/jwks.json", "self") is not None


@pytest.mark.asyncio
async def test_jwks_cache_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    import felix.auth.jwt as jm

    monkeypatch.setattr(jm, "_remote_jwks", {"u": ("keys", int(time.time() * 1000))})
    assert cached_jwks("u") == "keys"
    monkeypatch.setattr(jm, "_remote_jwks", {"u": ("keys", int(time.time() * 1000) - jm.JWKS_TTL_MS - 1)})
    assert cached_jwks("u") is None, "a stale key set must be refetched, not reused"


@pytest.mark.asyncio
async def test_jwks_fetch_failure_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    from felix.auth.jwt import refresh_jwks

    assert await refresh_jwks("https://127.0.0.1:1/nope", timeout_s=0.2) is None


# --- tenant resolution -----------------------------------------------------------


def _cfg(mode: str = "claim"):
    return parse_verifiers(f"self:{_ISS}")[0].__class__(
        scheme="self",
        issuer=_ISS,
        tenant_mode=mode,  # type: ignore[arg-type]
    )


def test_missing_tenant_claim_is_an_error_not_a_shared_default() -> None:
    """It fell back to the issuer host's first DNS label, so every user without a
    tenant claim landed in the same tenant — a cross-tenant path, not a default."""
    with pytest.raises(TenantResolutionError):
        _tenant_from_payload({"sub": "u"}, _cfg(), _settings())


def test_claimed_tenant_is_used_when_no_allowlist() -> None:
    assert _tenant_from_payload({"tenant_id": "acme"}, _cfg(), _settings()) == "acme"


def test_claimed_tenant_must_be_in_the_allowlist() -> None:
    s = _settings(allowed_tenants="acme,globex")
    assert _tenant_from_payload({"tenant_id": "acme"}, _cfg(), s) == "acme"
    with pytest.raises(TenantResolutionError, match="not in FELIX_ALLOWED_TENANTS"):
        _tenant_from_payload({"tenant_id": "evilcorp"}, _cfg(), s)


def test_cognito_custom_attribute_is_still_subject_to_the_allowlist() -> None:
    """`custom:*` attributes are frequently user-writable on Cognito."""
    s = _settings(allowed_tenants="acme")
    with pytest.raises(TenantResolutionError):
        _tenant_from_payload({"custom:tenant_id": "victim"}, _cfg(), s)


def test_fixed_mode_ignores_the_claim_entirely() -> None:
    cfg = parse_verifiers(f"self:{_ISS};tenant=fixed:pinned")[0]
    assert cfg.tenant_mode == "fixed"
    assert _tenant_from_payload({"tenant_id": "attacker"}, cfg, _settings()) == "pinned"


def test_malformed_tenant_spec_is_reported(caplog: pytest.LogCaptureFixture) -> None:
    """`;tenant=pinned` (missing the `fixed:` prefix) silently left tenant_mode=claim,
    so a pinned tenant was quietly downgraded to whatever the token said."""
    import logging

    with caplog.at_level(logging.ERROR, logger="felix.auth.jwt"):
        cfg = parse_verifiers(f"self:{_ISS};tenant=pinned")[0]
    assert cfg.tenant_mode == "claim"
    assert "unrecognised tenant spec" in caplog.text


def test_disallowed_tenant_fails_verification_with_a_clear_reason() -> None:
    s = _settings(allowed_tenants="acme")
    result = _verify(_mint(tenant_id="evilcorp", exp=int(time.time()) + 600), settings=s)
    assert result.reason == "tenant_not_allowed"  # type: ignore[union-attr]


# --- management scopes -----------------------------------------------------------


def test_mgmt_scope_check_fails_closed_without_settings() -> None:
    """Three chained getattr defaults landed on "none" = skip every check."""
    from fastapi import HTTPException
    from felix.auth.mgmt import require_mgmt_scopes

    class _NoState:
        app = None

    with pytest.raises(HTTPException) as e:
        require_mgmt_scopes(_NoState(), "audit:read")  # type: ignore[arg-type]
    assert e.value.status_code == 500
