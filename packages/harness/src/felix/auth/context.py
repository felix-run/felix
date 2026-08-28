"""Auth context — principal + outbound token helper."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

# The auth modes core resolves itself. `FELIX_AUTH_MODE` is an open string so a
# plugin can register another, but these are NOT overridable: letting an optional
# package redefine `api_key` would make a weaker authenticator installable by
# accident. Defined here rather than in `middleware` so the CLI and `config` can
# read it without importing Starlette.
BUILTIN_AUTH_MODES = frozenset({"none", "api_key", "jwt"})


@dataclass(slots=True)
class Principal:
    subject: str
    tenant_id: str
    scopes: frozenset[str] = field(default_factory=frozenset)
    issuer: str = ""
    # How the caller authenticated: "api_key", a JWT verifier scheme ("access",
    # "cognito", "self"), or "anonymous". Enforced by manifest `auth.inbound.schemes`.
    scheme: str = "anonymous"


OutboundTokenFn = Callable[[dict[str, str | None]], Awaitable[str]]


@dataclass(slots=True)
class AuthContext:
    principal: Principal
    outbound_token: OutboundTokenFn
    anonymous: bool = False
    raw_claims: dict[str, Any] = field(default_factory=dict)


async def _empty_outbound(_target: dict[str, str | None]) -> str:
    return ""


ANONYMOUS = AuthContext(
    principal=Principal(
        subject="", tenant_id="default", scopes=frozenset(), issuer="anonymous", scheme="anonymous"
    ),
    outbound_token=_empty_outbound,
    anonymous=True,
)


def require_scope(auth: AuthContext, *scopes: str) -> None:
    """Raise PermissionError when any required scope is missing."""
    if not scopes:
        return
    missing = [s for s in scopes if s not in auth.principal.scopes]
    if missing:
        raise PermissionError(f"missing scopes: {', '.join(missing)}")


def require_scopes(auth: AuthContext, scopes: list[str] | tuple[str, ...] | frozenset[str]) -> None:
    require_scope(auth, *scopes)


__all__ = [
    "ANONYMOUS",
    "AuthContext",
    "OutboundTokenFn",
    "Principal",
    "require_scope",
    "require_scopes",
]
