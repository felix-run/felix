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


# The tenant id is the ownership boundary, and it arrives from outside: an api-key
# `tenant_id` field or a JWT claim, and on Cognito `custom:*` claims are frequently
# user-writable. The prefix rule (`{tenant}:{suffix}` thread ids) only partitions while the
# delimiter cannot appear in the tenant itself — `acme` and `acme:sub` would otherwise both
# "own" the thread `acme:sub:x`.
#
# This was previously enforced only at the far end, when a thread id was built, so a bad
# tenant authenticated fine and failed later at a write. Validating at construction makes an
# unusable tenant unrepresentable instead.
TENANT_DELIMS = frozenset(":#")
MAX_TENANT_ID = 128


def assert_valid_tenant_id(tenant_id: str) -> None:
    """Raise `ValueError` unless `tenant_id` is a single delimiter-free segment."""
    if not tenant_id or tenant_id.strip() != tenant_id:
        raise ValueError("tenant_id must be a non-empty, unpadded string")
    if any(c in tenant_id for c in TENANT_DELIMS):
        raise ValueError(f"tenant_id may not contain {''.join(sorted(TENANT_DELIMS))!r}")
    if len(tenant_id) > MAX_TENANT_ID:
        raise ValueError(f"tenant_id exceeds {MAX_TENANT_ID} characters")


@dataclass(slots=True)
class Principal:
    subject: str
    tenant_id: str
    scopes: frozenset[str] = field(default_factory=frozenset)
    issuer: str = ""
    # How the caller authenticated: "api_key", a JWT verifier scheme ("access",
    # "cognito", "self"), or "anonymous". Enforced by manifest `auth.inbound.schemes`.
    scheme: str = "anonymous"

    def __post_init__(self) -> None:
        # Backstop for every construction path, including ones added later. The doors
        # validate too, so a caller gets a 401 rather than this exception.
        assert_valid_tenant_id(self.tenant_id)


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
