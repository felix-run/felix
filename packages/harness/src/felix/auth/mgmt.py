"""Management-API scope checks (manifests, audit, jobs, …).

When ``auth_mode`` is ``none``, checks are skipped for local DX.
When jwt/api_key is on, callers need the listed scopes (or ``admin`` / ``*``).
A ``*:write`` scope also satisfies the matching ``*:read``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from fastapi import HTTPException, Request

from felix.auth.context import ANONYMOUS, AuthContext

logger = logging.getLogger("felix.auth.mgmt")


def auth_from_request(request: Request) -> AuthContext:
    auth = getattr(request.state, "auth", None)
    return auth if isinstance(auth, AuthContext) else ANONYMOUS


def _scopes_of(auth: AuthContext) -> frozenset[str]:
    return frozenset(auth.principal.scopes or ())


def _satisfied(have: frozenset[str], needed: str) -> bool:
    if needed in have:
        return True
    if needed.endswith(":read"):
        write = f"{needed[:-5]}:write"
        if write in have:
            return True
    return False


def holds_mgmt_scopes(settings: Any, held: Iterable[str], *scopes: str) -> bool:
    """Whether a scope set satisfies management scopes — the question, not the refusal.

    Split out of `require_mgmt_scopes` so a caller that must *degrade* rather than 403 asks
    the same question the routes ask. The durable chat stream is that caller: it announces
    pending approvals to a client that may hold `approvals:read` and must simply stay quiet
    for one that does not, on a route whose own auth is the manifest's rather than a
    management scope. Two copies of "admin bypasses, `x:write` implies `x:read`, `auth_mode
    =none` checks nothing" is how one of them ends up subtly more permissive.

    Takes the scope set rather than an auth object on purpose: two different classes in this
    repo are named `AuthContext` — `felix.auth.context.AuthContext` holds a `.principal`, and
    the `felix.context.AuthContext` the chat routes carry holds `.scopes` directly. A
    parameter typed for one raises `AttributeError` on the other, and a *permission* check is
    the worst place to find out which one you were handed.
    """
    if not scopes:
        return True
    if getattr(settings, "auth_mode", "none") == "none":
        return True
    have = frozenset(held or ())
    if "admin" in have or "*" in have:
        return True
    return all(_satisfied(have, s) for s in scopes)


def require_mgmt_scopes(request: Request, *scopes: str) -> None:
    """Raise HTTP 403 when management scopes are missing under jwt/api_key auth."""
    if not scopes:
        return
    state = getattr(getattr(request, "app", None), "state", None)
    cfg = getattr(state, "settings", None) if state is not None else None
    if cfg is None:
        # Three chained getattr defaults used to land on "none" here, i.e. skip every
        # scope check. create_app sets app.state.settings eagerly, so a missing one means
        # a sub-app or plugin router mounted these routes without it — which must not
        # silently disable management authorization.
        logger.error("management scope check has no settings on app.state; denying")
        raise HTTPException(status_code=500, detail="auth_misconfigured")
    auth = auth_from_request(request)
    if holds_mgmt_scopes(cfg, _scopes_of(auth), *scopes):
        return
    have = _scopes_of(auth)
    missing = [s for s in scopes if not _satisfied(have, s)]
    raise HTTPException(status_code=403, detail=f"missing scopes: {', '.join(missing)}")


def tenant_id_from_request(request: Request) -> str:
    from felix.context import try_get_context

    ctx = try_get_context()
    if ctx is not None:
        return ctx.auth.tenant_id
    auth = auth_from_request(request)
    return auth.principal.tenant_id or "default"


def subject_from_request(request: Request) -> str:
    from felix.context import try_get_context

    ctx = try_get_context()
    if ctx is not None:
        return ctx.auth.principal_sub
    auth = auth_from_request(request)
    return auth.principal.subject or "anonymous"


# Documented management scopes (mint-jwt / API keys).
SCOPE_MANIFESTS_READ = "manifests:read"
SCOPE_MANIFESTS_WRITE = "manifests:write"
SCOPE_AUDIT_READ = "audit:read"
SCOPE_ARTIFACTS_READ = "artifacts:read"
SCOPE_APPROVALS_READ = "approvals:read"
SCOPE_APPROVALS_WRITE = "approvals:write"
SCOPE_JOBS_READ = "jobs:read"
SCOPE_JOBS_WRITE = "jobs:write"
SCOPE_PLANS_READ = "plans:read"
SCOPE_PLANS_WRITE = "plans:write"
SCOPE_EVAL_READ = "eval:read"
SCOPE_EVAL_WRITE = "eval:write"
SCOPE_USAGE_READ = "usage:read"
SCOPE_MEMORY_READ = "memory:read"
SCOPE_MEMORY_WRITE = "memory:write"
SCOPE_DOCUMENTS_READ = "documents:read"
SCOPE_DOCUMENTS_WRITE = "documents:write"
# Separate from `artifacts:read`, which reads tool output the harness itself spilled.
# These are caller-uploaded bytes with a caller-driven lifecycle, so the permission to
# write them is its own grant rather than a side effect of being able to read spill.
SCOPE_FILES_READ = "files:read"
SCOPE_FILES_WRITE = "files:write"

__all__ = [
    "SCOPE_APPROVALS_READ",
    "SCOPE_APPROVALS_WRITE",
    "SCOPE_ARTIFACTS_READ",
    "SCOPE_AUDIT_READ",
    "SCOPE_DOCUMENTS_READ",
    "SCOPE_DOCUMENTS_WRITE",
    "SCOPE_EVAL_READ",
    "SCOPE_EVAL_WRITE",
    "SCOPE_FILES_READ",
    "SCOPE_FILES_WRITE",
    "SCOPE_JOBS_READ",
    "SCOPE_JOBS_WRITE",
    "SCOPE_MANIFESTS_READ",
    "SCOPE_MANIFESTS_WRITE",
    "SCOPE_MEMORY_READ",
    "SCOPE_MEMORY_WRITE",
    "SCOPE_PLANS_READ",
    "SCOPE_PLANS_WRITE",
    "SCOPE_USAGE_READ",
    "auth_from_request",
    "holds_mgmt_scopes",
    "require_mgmt_scopes",
    "subject_from_request",
    "tenant_id_from_request",
]
