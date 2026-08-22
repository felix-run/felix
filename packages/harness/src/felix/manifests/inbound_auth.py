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


def enforce_inbound_auth(manifest: Manifest, auth: Any) -> None:
    """Raise ``InboundAuthError`` when anonymous or scopes violate the manifest.

    Accepts either ``felix.context.AuthContext`` or ``felix.auth.context.AuthContext``.
    """
    inbound = manifest.spec.auth.inbound
    anonymous = bool(getattr(auth, "anonymous", True))
    if not inbound.allow_anonymous and anonymous:
        raise InboundAuthError("anonymous_not_allowed", status_code=401)
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
