"""Enforce manifest ``spec.auth.inbound`` against the request principal."""

from __future__ import annotations

from typing import Any

from felix.manifests.schema import Manifest


class InboundAuthError(PermissionError):
    """Manifest inbound auth policy denied the caller."""

    def __init__(self, detail: str, *, status_code: int = 401) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def _scopes_of(auth: Any) -> frozenset[str]:
    scopes = getattr(auth, "scopes", None)
    if scopes is not None:
        return frozenset(scopes)
    principal = getattr(auth, "principal", None)
    if principal is not None:
        return frozenset(getattr(principal, "scopes", ()) or ())
    return frozenset()


# "jwt" is an umbrella for the configured JWT verifier schemes, so a manifest can say
# `schemes: [jwt]` without naming every IdP it might be deployed against.
_JWT_SCHEMES = frozenset({"access", "cognito", "self"})


def _scheme_of(auth: Any) -> str:
    scheme = getattr(auth, "scheme", None)
    if scheme is None:
        principal = getattr(auth, "principal", None)
        scheme = getattr(principal, "scheme", None) if principal is not None else None
    return str(scheme or "anonymous").lower()


def _expand_schemes(allowed: list[str]) -> frozenset[str]:
    out = set(allowed)
    if "jwt" in out:
        out |= _JWT_SCHEMES
    if "api_key" in out:
        out.add("apikey")
    if "apikey" in out:
        out.add("api_key")
    return frozenset(out)


def enforce_inbound_auth(manifest: Manifest, auth: Any) -> None:
    """Raise ``InboundAuthError`` when anonymous or scopes violate the manifest.

    Accepts either ``felix.context.AuthContext`` or ``felix.auth.context.AuthContext``.
    """
    inbound = manifest.spec.auth.inbound
    anonymous = bool(getattr(auth, "anonymous", True))
    if not inbound.allow_anonymous and anonymous:
        raise InboundAuthError("anonymous_not_allowed", status_code=401)
    # `schemes` was previously only a compile-time check that *something* was set; it
    # never constrained the caller. A manifest naming ["jwt"] accepted an api_key
    # principal, so the field looked like an access control and was not one.
    allowed = [str(x).strip().lower() for x in (inbound.schemes or []) if str(x).strip()]
    if allowed and not anonymous:
        scheme = _scheme_of(auth)
        if scheme not in _expand_schemes(allowed):
            raise InboundAuthError(
                f"auth scheme {scheme!r} not permitted (allowed: {', '.join(sorted(allowed))})",
                status_code=403,
            )

    required = list(inbound.required_scopes or [])
    if required:
        have = _scopes_of(auth)
        missing = [s for s in required if s not in have]
        if missing:
            raise InboundAuthError(
                f"missing scopes: {', '.join(missing)}",
                status_code=403,
            )


__all__ = ["InboundAuthError", "enforce_inbound_auth"]
