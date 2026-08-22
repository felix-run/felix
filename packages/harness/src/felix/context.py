"""Request-scoped context via contextvars (AsyncLocalStorage equivalent)."""

from __future__ import annotations

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


@dataclass
class LimitState:
    tool_calls: int = 0
    peer_hops: int = 0
    started_at_ms: int = 0
    audit_count: int = 0
    tokens_input: int = 0
    tokens_output: int = 0
    aborted: bool = False


@dataclass
class RequestContext:
    settings: Settings
    auth: AuthContext
    limit_state: LimitState = field(default_factory=LimitState)
    manifest_id: str = ""
    thread_id: str | None = None
    unattended: bool = False
    extras: dict[str, Any] = field(default_factory=dict)


_ctx: ContextVar[RequestContext | None] = ContextVar("felix_request_context", default=None)


def get_context() -> RequestContext:
    ctx = _ctx.get()
    if ctx is None:
        raise RuntimeError("No Felix RequestContext installed")
    return ctx


def try_get_context() -> RequestContext | None:
    return _ctx.get()


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
            _ctx.reset(token)
