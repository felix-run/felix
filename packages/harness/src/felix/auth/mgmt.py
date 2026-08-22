"""Management-API scope checks (manifests, audit, jobs, …).

When ``auth_mode`` is ``none``, checks are skipped for local DX.
When jwt/api_key is on, callers need the listed scopes (or ``admin`` / ``*``).
A ``*:write`` scope also satisfies the matching ``*:read``.
"""

from __future__ import annotations

from fastapi import HTTPException, Request

from felix.auth.context import ANONYMOUS, AuthContext


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


def require_mgmt_scopes(request: Request, *scopes: str) -> None:
    """Raise HTTP 403 when management scopes are missing under jwt/api_key auth."""
    if not scopes:
        return
    settings = getattr(getattr(request, "app", None), "state", None)
    cfg = getattr(settings, "settings", None) if settings is not None else None
    mode = getattr(cfg, "auth_mode", "none") if cfg is not None else "none"
    if mode == "none":
        return
    auth = auth_from_request(request)
    have = _scopes_of(auth)
    if "admin" in have or "*" in have:
        return
    missing = [s for s in scopes if not _satisfied(have, s)]
    if missing:
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
SCOPE_APPROVALS_READ = "approvals:read"
SCOPE_APPROVALS_WRITE = "approvals:write"
SCOPE_JOBS_READ = "jobs:read"
SCOPE_JOBS_WRITE = "jobs:write"
SCOPE_PLANS_READ = "plans:read"
SCOPE_PLANS_WRITE = "plans:write"
SCOPE_EVAL_READ = "eval:read"
SCOPE_EVAL_WRITE = "eval:write"
SCOPE_USAGE_READ = "usage:read"

__all__ = [
    "SCOPE_APPROVALS_READ",
    "SCOPE_APPROVALS_WRITE",
    "SCOPE_AUDIT_READ",
    "SCOPE_EVAL_READ",
    "SCOPE_EVAL_WRITE",
    "SCOPE_JOBS_READ",
    "SCOPE_JOBS_WRITE",
    "SCOPE_MANIFESTS_READ",
    "SCOPE_MANIFESTS_WRITE",
    "SCOPE_PLANS_READ",
    "SCOPE_PLANS_WRITE",
    "SCOPE_USAGE_READ",
    "auth_from_request",
    "require_mgmt_scopes",
    "subject_from_request",
    "tenant_id_from_request",
]
