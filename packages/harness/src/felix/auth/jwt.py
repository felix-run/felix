"""JWT verification — Access / OIDC / self-issued JWKS."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Literal

from joserfc import jwk, jwt
from joserfc.errors import JoseError
from joserfc.jwt import JWTClaimsRegistry

from felix.auth.context import Principal, assert_valid_tenant_id

if TYPE_CHECKING:
    from felix.config import Settings

logger = logging.getLogger("felix.auth.jwt")

ALLOWED_ALGORITHMS = ("RS256", "RS384", "RS512", "ES256", "ES384", "ES512")


@dataclass(slots=True)
class VerifierConfig:
    scheme: Literal["access", "cognito", "self"]
    issuer: str
    audience: str | None = None
    tenant_mode: Literal["claim", "issuer", "fixed"] = "claim"
    fixed_tenant: str | None = None


@dataclass(slots=True)
class VerifyOk:
    ok: Literal[True] = True
    principal: Principal = None  # type: ignore[assignment]
    payload: dict[str, Any] = None  # type: ignore[assignment]


@dataclass(slots=True)
class VerifyFail:
    ok: Literal[False] = False
    reason: Literal["invalid_token", "expired", "no_verifier_matched"] = "invalid_token"


VerifyResult = VerifyOk | VerifyFail


@lru_cache(maxsize=4)
def parse_verifiers(jwt_verifiers: str) -> list[VerifierConfig]:
    """Parse FELIX_JWT_VERIFIERS: comma-separated scheme:issuer[:audience][;tenant=…]."""
    out: list[VerifierConfig] = []
    if not jwt_verifiers.strip():
        return out
    for part in jwt_verifiers.split(","):
        part = part.strip()
        if not part:
            continue
        tenant_mode: Literal["claim", "issuer", "fixed"] = "claim"
        fixed_tenant: str | None = None
        if ";tenant=" in part:
            part, tenant_spec = part.split(";tenant=", 1)
            tenant_spec = tenant_spec.strip()
            if tenant_spec.startswith("fixed:"):
                tenant_mode = "fixed"
                fixed_tenant = tenant_spec.removeprefix("fixed:")
            elif tenant_spec in {"claim", "issuer"}:
                tenant_mode = tenant_spec  # type: ignore[assignment]
            else:
                # Silently falling through left tenant_mode="claim", so a typo in the
                # verifier spec quietly downgraded a pinned tenant to a token claim.
                logger.error(
                    "unrecognised tenant spec %r in FELIX_JWT_VERIFIERS; expected "
                    "'claim', 'issuer', or 'fixed:<tenant>'",
                    tenant_spec,
                )
        bits = part.split(":")
        if len(bits) < 2:
            continue
        scheme = bits[0].strip()
        if scheme not in {"access", "cognito", "self"}:
            # Silently dropping this left an operator with a verifier that simply
            # never matched and no indication why.
            logger.warning(
                "ignoring JWT verifier with unknown scheme %r (expected access|cognito|self): %s",
                scheme,
                part,
            )
            continue
        # issuer may contain colons (https://…)
        rest = part[len(scheme) + 1 :]
        audience: str | None = None
        if ";aud=" in rest:
            rest, audience = rest.split(";aud=", 1)
            audience = audience.strip() or None
        out.append(
            VerifierConfig(
                scheme=scheme,  # type: ignore[arg-type]
                issuer=rest.strip(),
                audience=audience,
                tenant_mode=tenant_mode,
                fixed_tenant=fixed_tenant,
            )
        )
    return out


def _jwks_url(cfg: VerifierConfig) -> str:
    if cfg.scheme == "access":
        return f"https://{cfg.issuer}/cdn-cgi/access/certs"
    return f"{cfg.issuer.rstrip('/')}/.well-known/jwks.json"


def _issuer(cfg: VerifierConfig) -> str:
    if cfg.scheme == "access":
        return f"https://{cfg.issuer}"
    return cfg.issuer


class TenantResolutionError(ValueError):
    """The token does not identify a tenant this deployment will accept."""


def allowed_tenants(settings: Settings | None = None) -> frozenset[str]:
    if settings is None:
        from felix.config import get_settings

        settings = get_settings()
    raw = getattr(settings, "allowed_tenants", "") or ""
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def _tenant_from_payload(
    payload: dict[str, Any], cfg: VerifierConfig, settings: Settings | None = None
) -> str:
    if cfg.tenant_mode == "fixed" and cfg.fixed_tenant:
        return cfg.fixed_tenant
    if cfg.tenant_mode == "issuer":
        host = _issuer(cfg).removeprefix("https://").removeprefix("http://").split("/")[0]
        return host.split(".")[0] or "default"

    claimed = ""
    for key in ("tenant_id", "custom:tenant_id", "tid"):
        val = payload.get(key)
        if isinstance(val, str) and val:
            claimed = val
            break

    if not claimed:
        # Previously this fell back to the issuer host's first DNS label, so every user
        # whose token carried no tenant claim silently landed in the *same* tenant —
        # which is a cross-tenant data path, not a default.
        raise TenantResolutionError(
            f"token from {_issuer(cfg)} has no tenant claim and the verifier uses "
            "tenant_mode=claim; set tenant_mode=fixed:<tenant> or issue the claim"
        )

    try:
        # Same rule as every other door. A claim is the least trustworthy source of a
        # tenant id, so the shape is checked before the allowlist.
        assert_valid_tenant_id(claimed)
    except ValueError as exc:
        raise TenantResolutionError(f"tenant claim is unusable: {exc}") from exc

    allowed = allowed_tenants(settings)
    if allowed and claimed not in allowed:
        # tenant_id is the isolation boundary and it arrives in a token claim. On Cognito
        # `custom:*` attributes are frequently user-writable, so an allowlist is the only
        # server-side check available without a tenant registry.
        raise TenantResolutionError(f"tenant {claimed!r} is not in FELIX_ALLOWED_TENANTS")
    return claimed


def _scopes_from_payload(payload: dict[str, Any]) -> frozenset[str]:
    raw = payload.get("scope") or payload.get("scopes") or []
    if isinstance(raw, str):
        return frozenset(s for s in raw.split() if s)
    if isinstance(raw, list):
        return frozenset(str(s) for s in raw)
    return frozenset()


def payload_to_principal(
    payload: dict[str, Any], cfg: VerifierConfig, settings: Settings | None = None
) -> Principal:
    sub = str(payload.get("sub") or payload.get("email") or "")
    return Principal(
        subject=sub,
        tenant_id=_tenant_from_payload(payload, cfg, settings),
        scopes=_scopes_from_payload(payload),
        issuer=str(payload.get("iss") or _issuer(cfg)),
        scheme=cfg.scheme,
    )


# url -> (key_set, fetched_at_ms)
_remote_jwks: dict[str, tuple[Any, int]] = {}

# How long a fetched key set is reused before refetching. Short enough to pick up a
# rotation within an hour, long enough that verification is not an HTTP call per request.
JWKS_TTL_MS = 15 * 60 * 1000
JWKS_FETCH_TIMEOUT_S = 5.0

# Schemes whose keys live at the issuer, not in local configuration.
_REMOTE_SCHEMES = frozenset({"access", "cognito"})


def _import_local_key_set(jwks_public: str) -> Any:
    """Parse FELIX_JWKS_PUBLIC as either a JWKS document or a PEM public key."""
    if not jwks_public.strip():
        return None
    try:
        if jwks_public.strip().startswith("{"):
            return jwk.KeySet.import_key_set(json.loads(jwks_public))
        return jwk.import_key(jwks_public.strip(), "RSA")
    except Exception:
        logger.error("FELIX_JWKS_PUBLIC could not be imported", exc_info=True)
        return None


async def refresh_jwks(url: str, *, timeout_s: float = JWKS_FETCH_TIMEOUT_S) -> Any:
    """Fetch and cache an issuer's key set.

    Remote JWKS was never fetched — the function carried a literal
    "Lazy remote fetch via httpx would go here" comment — so the `access` and `cognito`
    schemes had no working key source at all. Worse, the fallback returned
    FELIX_JWKS_PUBLIC regardless of the URL, so the *local self-signing key* was used to
    verify tokens that claimed to come from Cloudflare Access or Cognito. That was safe
    only because the issuer claim still had to match.
    """
    import httpx

    try:
        async with httpx.AsyncClient(timeout=timeout_s, follow_redirects=False) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            key_set = jwk.KeySet.import_key_set(resp.json())
    except Exception:
        logger.error("JWKS fetch failed for %s", url, exc_info=True)
        return None
    _remote_jwks[url] = (key_set, int(time.time() * 1000))
    logger.info("JWKS refreshed for %s", url)
    return key_set


def cached_jwks(url: str) -> Any:
    """A cached key set if it is still fresh, else None."""
    entry = _remote_jwks.get(url)
    if entry is None:
        return None
    key_set, fetched_at = entry
    if int(time.time() * 1000) - fetched_at > JWKS_TTL_MS:
        return None
    return key_set


def _load_key_set(jwks_public: str, url: str, scheme: str = "self") -> Any:
    """Key set for one verifier.

    Local configuration is used **only** for the `self` scheme. A remote issuer's tokens
    are verified against that issuer's published keys or not at all.
    """
    if scheme in _REMOTE_SCHEMES:
        return cached_jwks(url)
    return _import_local_key_set(jwks_public)


async def refresh_all_jwks(settings: Settings) -> int:
    """Fetch every configured remote issuer's key set. Returns how many succeeded."""
    ok = 0
    for cfg in parse_verifiers(settings.jwt_verifiers):
        if cfg.scheme not in _REMOTE_SCHEMES:
            continue
        if await refresh_jwks(_jwks_url(cfg)) is not None:
            ok += 1
    return ok


async def run_jwks_refresh_loop(settings: Settings, *, interval_s: float) -> None:
    """Keep remote key sets fresh so verification never has to fetch inline.

    `verify_jwt` is synchronous and on the request path, so it reads a cache rather than
    making an HTTP call; something has to fill that cache.
    """
    import asyncio

    while True:
        try:
            await asyncio.sleep(interval_s)
            await refresh_all_jwks(settings)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("jwks refresh iteration failed", exc_info=True)


def verify_jwt(
    token: str,
    configs: list[VerifierConfig],
    *,
    jwks_public: str = "",
    settings: Settings | None = None,
) -> VerifyResult:
    if not configs:
        return VerifyFail(reason="no_verifier_matched")
    saw_expired = False
    for cfg in configs:
        key_set = _load_key_set(jwks_public, _jwks_url(cfg), cfg.scheme)
        if key_set is None:
            if cfg.scheme in _REMOTE_SCHEMES:
                logger.warning("no cached JWKS for %s; call refresh_jwks() at startup", _jwks_url(cfg))
            continue

        # A shared issuer (Cloudflare Access, a Cognito user pool) signs tokens for every
        # application under it. Without an audience check, a token minted for any other
        # app at the same issuer is accepted here.
        if cfg.scheme in _REMOTE_SCHEMES and not cfg.audience:
            logger.error(
                "verifier %s:%s has no audience; refusing to accept tokens from a shared issuer without one",
                cfg.scheme,
                cfg.issuer,
            )
            continue

        try:
            # `exp` is essential. joserfc validates it only when present, so a token
            # minted without an expiry was previously accepted forever.
            registry_claims: dict[str, Any] = {
                "iss": {"essential": True, "value": _issuer(cfg)},
                "exp": {"essential": True},
            }
            if cfg.audience:
                registry_claims["aud"] = {"essential": True, "value": cfg.audience}
            claims_requests = JWTClaimsRegistry(**registry_claims)
            token_obj = jwt.decode(token, key_set, algorithms=list(ALLOWED_ALGORITHMS))
            claims_requests.validate(token_obj.claims)
            try:
                principal = payload_to_principal(dict(token_obj.claims), cfg, settings)
            except TenantResolutionError as exc:
                logger.warning("tenant resolution refused a valid token: %s", exc)
                return VerifyFail(reason="tenant_not_allowed")
            return VerifyOk(principal=principal, payload=dict(token_obj.claims))
        except JoseError as exc:
            msg = str(exc).lower()
            # Was `"expired" in msg or "exp" in msg`, and "exp" is a substring of
            # "unexpected" — so signature failures were reported as expiry.
            if "expired" in msg or "exp_claim" in msg or msg.strip().startswith("exp"):
                saw_expired = True
                continue
            # claim mismatch → try next verifier
            continue
        except Exception:
            continue
    if saw_expired:
        return VerifyFail(reason="expired")
    return VerifyFail(reason="invalid_token")


def mint_token(
    settings: Settings,
    *,
    sub: str,
    tenant_id: str,
    scopes: list[str],
    ttl_seconds: int = 3600,
) -> str:
    """Mint a self-issued JWT for local dev / CLI."""
    if not settings.jwks_private.strip():
        raise RuntimeError("FELIX_JWKS_PRIVATE is required to mint tokens")
    key = jwk.import_key(settings.jwks_private.strip(), "RSA")
    now = int(time.time())
    claims = {
        "sub": sub,
        "tenant_id": tenant_id,
        "scope": " ".join(scopes),
        "iss": "felix-self",
        "iat": now,
        "exp": now + ttl_seconds,
    }
    return jwt.encode({"alg": "RS256"}, claims, key)


def public_jwks(jwks_public: str) -> dict[str, Any]:
    """Normalize FELIX_JWKS_PUBLIC (JWKS JSON or PEM) into a JWKS document."""
    raw = (jwks_public or "").strip()
    if not raw:
        return {"keys": []}
    if raw.startswith("{"):
        data = json.loads(raw)
        if isinstance(data, dict) and "keys" in data:
            return data
        if isinstance(data, dict) and data.get("kty"):
            return {"keys": [data]}
        return {"keys": []}
    for kty in ("RSA", "EC", "OKP"):
        try:
            key = jwk.import_key(raw, kty)
            doc = key.as_dict(private=False)
            doc.setdefault("kid", "felix-self")
            if kty == "RSA":
                doc.setdefault("alg", "RS256")
                doc.setdefault("use", "sig")
            return {"keys": [doc]}
        except Exception:
            continue
    logger.warning("FELIX_JWKS_PUBLIC is neither JWKS JSON nor a supported PEM key")
    return {"keys": []}


__all__ = [
    "VerifierConfig",
    "VerifyFail",
    "VerifyOk",
    "VerifyResult",
    "mint_token",
    "parse_verifiers",
    "payload_to_principal",
    "public_jwks",
    "verify_jwt",
]
