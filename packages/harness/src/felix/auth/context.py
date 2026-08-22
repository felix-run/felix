"""Auth context — principal + outbound token helper."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Principal:
    subject: str
    tenant_id: str
    scopes: frozenset[str] = field(default_factory=frozenset)
    issuer: str = ""


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
    principal=Principal(subject="", tenant_id="default", scopes=frozenset(), issuer="anonymous"),
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
