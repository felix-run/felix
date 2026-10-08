"""Request-scoped context via contextvars (AsyncLocalStorage equivalent)."""

from __future__ import annotations

import time
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from felix.config import Settings


@dataclass
class AuthContext:
    principal_sub: str = "anonymous"
    tenant_id: str = "default"
    scopes: frozenset[str] = field(default_factory=frozenset)
    anonymous: bool = True
    raw_claims: dict[str, Any] = field(default_factory=dict)
    # How the caller authenticated, carried from felix.auth.context.Principal so
    # manifest `auth.inbound.schemes` can be enforced on the request path.
    scheme: str = "anonymous"
    # Set only when a machine actor is running work a human started — today, a resumed durable
    # fiber. `principal_sub` stays the machine (`fiber`, `cron`, `eval`, `a2a`) so an audit row
    # never claims a person took an action a worker took minutes later; `on_behalf_of` carries
    # who it is for, which is what `bind_principal` needs to keep an approval valid across a
    # resume. Authorization reads it deliberately and in one place; audit does not.
    on_behalf_of: str = ""
    # The caller's personal skill library (`skills.library_keys.personal_owner`), worked out once
    # from the verified principal where its issuer is still known. None for an anonymous caller
    # and for every machine actor but a resumed fiber, which carries its starter's: a compile
    # with None loads the tenant's library and no one's own.
    skill_owner: str | None = None


@dataclass
class LimitState:
    tool_calls: int = 0
    peer_hops: int = 0
    # Wall-clock origin for `limits.max_wall_clock_seconds`. Defaults to the moment the
    # state is constructed so a deadline is measurable even if nobody sets it explicitly.
    started_at_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    audit_count: int = 0
    tokens_input: int = 0
    tokens_output: int = 0
    # The part of `tokens_input` served from the prompt cache, for wires that report it.
    tokens_cached: int = 0
    cost_usd: float = 0.0
    aborted: bool = False
    # Why the run was aborted, surfaced to the model and the caller.
    abort_reason: str = ""

    def elapsed_ms(self, now: int | None = None) -> int:
        return (now if now is not None else int(time.time() * 1000)) - self.started_at_ms


@dataclass
class RequestContext:
    settings: Settings
    auth: AuthContext
    limit_state: LimitState = field(default_factory=LimitState)
    manifest_id: str = ""
    thread_id: str | None = None
    unattended: bool = False
    extras: dict[str, Any] = field(default_factory=dict)
    # `spec.observability.metrics`: when non-empty, only these counter names are
    # recorded for this manifest. Empty means record everything (the default).
    metric_names: frozenset[str] = field(default_factory=frozenset)


_ctx: ContextVar[RequestContext | None] = ContextVar("felix_request_context", default=None)
# `felix.tools.workspace_hosted.WRITTEN_SCOPES_KEY`, spelled here so the context module imports
# nothing from the tools to check it.
_WORKSPACE_WRITTEN = "workspace_written_scopes"


def get_context() -> RequestContext:
    ctx = _ctx.get()
    if ctx is None:
        raise RuntimeError("No Felix RequestContext installed")
    return ctx


def try_get_context() -> RequestContext | None:
    return _ctx.get()


def current_tenant() -> tuple[Settings, str] | None:
    """The request's settings and tenant, or None outside a request or for a tenantless one.

    Typed, so a caller stops spelling `getattr(getattr(ctx, "auth", None), "tenant_id", None)`
    and choosing its own fallback when settings are missing: there are none to miss here.
    """
    ctx = _ctx.get()
    if ctx is None or not ctx.auth.tenant_id:
        return None
    return ctx.settings, str(ctx.auth.tenant_id)


@contextmanager
def run_with_context(ctx: RequestContext) -> Iterator[RequestContext]:
    from felix.db.session import rls_tenant

    token = _ctx.set(ctx)
    with rls_tenant(ctx.auth.tenant_id):
        try:
            yield ctx
        finally:
            _ctx.reset(token)


@asynccontextmanager
async def async_run_with_context(ctx: RequestContext) -> AsyncIterator[RequestContext]:
    from felix.db.session import rls_tenant

    token = _ctx.set(ctx)
    with rls_tenant(ctx.auth.tenant_id):
        try:
            yield ctx
        finally:
            try:
                if ctx.extras.get(_WORKSPACE_WRITTEN):
                    # The scopes this request wrote under the hosted workspace backend, backed up
                    # as it ends. Never raises (`checkpoint_written`).
                    from felix.tools.workspace_hosted import checkpoint_written

                    await checkpoint_written(ctx)
            finally:
                _ctx.reset(token)
