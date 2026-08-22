"""JWT verification — Access / OIDC / self-issued JWKS."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from joserfc import jwk, jwt
from joserfc.errors import JoseError
from joserfc.jwt import JWTClaimsRegistry

from felix.auth.context import Principal

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
        bits = part.split(":")
        if len(bits) < 2:
            continue
        scheme = bits[0].strip()
        if scheme not in {"access", "cognito", "self"}:
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


def _tenant_from_payload(payload: dict[str, Any], cfg: VerifierConfig) -> str:
    if cfg.tenant_mode == "fixed" and cfg.fixed_tenant:
        return cfg.fixed_tenant
    if cfg.tenant_mode == "issuer":
        host = _issuer(cfg).removeprefix("https://").removeprefix("http://").split("/")[0]
        return host.split(".")[0] or "default"
    for key in ("tenant_id", "custom:tenant_id", "tid"):
        val = payload.get(key)
        if isinstance(val, str) and val:
            return val
    host = _issuer(cfg).removeprefix("https://").removeprefix("http://").split("/")[0]
    return host.split(".")[0] or "default"


def _scopes_from_payload(payload: dict[str, Any]) -> frozenset[str]:
    raw = payload.get("scope") or payload.get("scopes") or []
    if isinstance(raw, str):
        return frozenset(s for s in raw.split() if s)
    if isinstance(raw, list):
        return frozenset(str(s) for s in raw)
    return frozenset()


def payload_to_principal(payload: dict[str, Any], cfg: VerifierConfig) -> Principal:
    sub = str(payload.get("sub") or payload.get("email") or "")
    return Principal(
        subject=sub,
        tenant_id=_tenant_from_payload(payload, cfg),
        scopes=_scopes_from_payload(payload),
        issuer=str(payload.get("iss") or _issuer(cfg)),
    )


_remote_jwks: dict[str, Any] = {}


def _load_key_set(jwks_public: str, url: str) -> Any:
    if jwks_public and url.endswith("/.well-known/jwks.json"):
        try:
            data = json.loads(jwks_public)
            return jwk.KeySet.import_key_set(data)
        except Exception:
            logger.debug("JWKS_PUBLIC parse failed; falling through", exc_info=True)
    # Lazy remote fetch via httpx would go here; for self/local we use JWKS_PUBLIC.
    # Without a key set, verification fails closed.
    cached = _remote_jwks.get(url)
    if cached is not None:
        return cached
    if jwks_public:
        try:
            data = json.loads(jwks_public) if jwks_public.strip().startswith("{") else None
            if data:
                ks = jwk.KeySet.import_key_set(data)
                _remote_jwks[url] = ks
                return ks
            # PEM public key
            key = jwk.import_key(jwks_public, "RSA")
            _remote_jwks[url] = key
            return key
        except Exception:
            logger.debug("failed to import JWKS_PUBLIC", exc_info=True)
    return None


def verify_jwt(
    token: str,
    configs: list[VerifierConfig],
    *,
    jwks_public: str = "",
) -> VerifyResult:
    if not configs:
        return VerifyFail(reason="no_verifier_matched")
    saw_expired = False
    for cfg in configs:
        key_set = _load_key_set(jwks_public, _jwks_url(cfg))
        if key_set is None and cfg.scheme != "self":
            continue
        if key_set is None:
            continue
        try:
            claims_requests = JWTClaimsRegistry(
                iss={"essential": True, "value": _issuer(cfg)},
            )
            if cfg.audience:
                claims_requests = JWTClaimsRegistry(
                    iss={"essential": True, "value": _issuer(cfg)},
                    aud={"essential": True, "value": cfg.audience},
                )
            token_obj = jwt.decode(token, key_set, algorithms=list(ALLOWED_ALGORITHMS))
            claims_requests.validate(token_obj.claims)
            principal = payload_to_principal(dict(token_obj.claims), cfg)
            return VerifyOk(principal=principal, payload=dict(token_obj.claims))
        except JoseError as exc:
            msg = str(exc).lower()
            if "expired" in msg or "exp" in msg:
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
