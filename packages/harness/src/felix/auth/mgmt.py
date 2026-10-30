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


def request_person(request: Request) -> tuple[str, str]:
    """The caller's tenant and person key (`issuer|subject`); 403 `not_a_person` for a caller that
    names no one person -- anonymous, a sub-less API key, a workflow, a machine actor. The one rule
    for who may keep a connection, shared by core's connection routes and a plugin's."""
    from felix.auth.persons import caller_person

    auth = auth_from_request(request)
    principal = auth.principal
    person = caller_person(issuer=principal.issuer, subject=principal.subject or "", anonymous=auth.anonymous)
    if not person:
        raise HTTPException(status_code=403, detail="not_a_person")
    return principal.tenant_id, person


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
    if is_operator(settings, held):
        return True
    have = frozenset(held or ())
    return all(_satisfied(have, s) for s in scopes)


def is_operator(settings: Any, held: Iterable[str]) -> bool:
    """Whether a scope set is an operator's: `admin` or `*`, or anything under `auth_mode=none`,
    where every caller is the one anonymous operator. The bypass every management check grants,
    asked on its own by the checks that are not about a scope -- whose approval, whose checkout,
    whose job."""
    if getattr(settings, "auth_mode", "none") == "none":
        return True
    have = frozenset(held or ())
    return SCOPE_ADMIN in have or "*" in have


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
SCOPE_ADMIN = "admin"
SCOPE_MANIFESTS_READ = "manifests:read"
SCOPE_MANIFESTS_WRITE = "manifests:write"
SCOPE_AUDIT_READ = "audit:read"
SCOPE_ARTIFACTS_READ = "artifacts:read"
SCOPE_APPROVALS_READ = "approvals:read"
SCOPE_APPROVALS_WRITE = "approvals:write"
SCOPE_JOBS_READ = "jobs:read"
SCOPE_JOBS_WRITE = "jobs:write"
# A person turning their own heartbeat on or off (`/heartbeats`). Its own grant rather than part of
# `jobs:write`: it writes a job only of the operator's design (`spec.heartbeat`), for the caller
# alone, so an operator can hand it to people who may not write arbitrary jobs.
SCOPE_HEARTBEATS_READ = "heartbeats:read"
SCOPE_HEARTBEATS_WRITE = "heartbeats:write"
SCOPE_PLANS_READ = "plans:read"
SCOPE_PLANS_WRITE = "plans:write"
# Authored workflows (`/workflows`). Writing one names manifests and tools its runs will call, so
# it is a grant of its own rather than a side effect of `manifests:write` or `jobs:write`.
SCOPE_WORKFLOWS_READ = "workflows:read"
SCOPE_WORKFLOWS_WRITE = "workflows:write"
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
# Separate from `manifests:read` rather than folded into it. A skill *body* is prompt
# content -- `activate_skill` hands it to the model as instructions to follow -- so reading
# one is reading instructions the agent will follow, which is a different question from
# reading the manifest that names it. An operator auditing prompts should be able to hold
# this without also holding the scope that lists every manifest.
SCOPE_SKILLS_READ = "skills:read"
# Writing the tenant skill library (`/skill-library`): save, publish, roll back, reject and
# archive. Publishing puts text in front of every manifest in the tenant as instructions, so
# it is a grant of its own rather than a side effect of `manifests:write`. Implies
# `skills:read` through the usual rule.
SCOPE_SKILLS_WRITE = "skills:write"
# Writing one's own personal skill library (`/skill-library/~me`). Its own grant, not implied by
# anything but `admin` / `*`: a personal skill reaches only its owner's turns, but an owner is a
# principal, and a shared service credential is one principal for everyone holding it -- so who
# may author instructions their turns will follow is an operator's decision per credential.
SCOPE_SKILLS_PERSONAL = "skills:personal"
# Running a `disable-model-invocation` skill as `/name` on `/v1`, whose caller is as often an agent
# as a person (`skills/invocation.py`). Implied by nothing but `admin` / `*`: a manual-only skill is
# one its author kept from the model, and an agent on the other end of `/v1` is a model.
SCOPE_SKILLS_INVOKE = "skills:invoke"
# `GET /metrics`. Its label values span every tenant (manifest ids, remote MCP tool names), so
# reading it is an operator grant, never something any valid credential carries by default.
SCOPE_METRICS_READ = "metrics:read"

__all__ = [
    "SCOPE_ADMIN",
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
    "SCOPE_HEARTBEATS_READ",
    "SCOPE_HEARTBEATS_WRITE",
    "SCOPE_JOBS_READ",
    "SCOPE_JOBS_WRITE",
    "SCOPE_MANIFESTS_READ",
    "SCOPE_MANIFESTS_WRITE",
    "SCOPE_MEMORY_READ",
    "SCOPE_MEMORY_WRITE",
    "SCOPE_METRICS_READ",
    "SCOPE_PLANS_READ",
    "SCOPE_PLANS_WRITE",
    "SCOPE_SKILLS_INVOKE",
    "SCOPE_SKILLS_PERSONAL",
    "SCOPE_SKILLS_READ",
    "SCOPE_SKILLS_WRITE",
    "SCOPE_USAGE_READ",
    "SCOPE_WORKFLOWS_READ",
    "SCOPE_WORKFLOWS_WRITE",
    "auth_from_request",
    "holds_mgmt_scopes",
    "is_operator",
    "require_mgmt_scopes",
    "subject_from_request",
    "tenant_id_from_request",
]
