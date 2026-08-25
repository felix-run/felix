"""Async SQLAlchemy engine / session factory from Settings."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from functools import lru_cache

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session

from felix.config import Settings, get_settings

logger = logging.getLogger("felix.db.session")

_rls_tenant: ContextVar[str | None] = ContextVar("felix_rls_tenant", default=None)
_rls_bypass: ContextVar[bool] = ContextVar("felix_rls_bypass", default=False)
_listener_installed = False


def _use_memory(settings: Settings | None = None) -> bool:
    """True when control-plane stores should use in-process dict backends."""
    url = (settings or get_settings()).database_url
    return url.startswith("memory://")


def _normalize_url(url: str) -> str:
    """Ensure an async driver is present for SQLAlchemy asyncio."""
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg://", 1)
    return url


@contextmanager
def rls_tenant(tenant_id: str | None) -> Iterator[None]:
    """Bind ``app.tenant_id`` for the current task (when ``FELIX_DATABASE_RLS``)."""
    token = _rls_tenant.set(tenant_id)
    try:
        yield
    finally:
        _rls_tenant.reset(token)


@contextmanager
def rls_bypass() -> Iterator[None]:
    """Allow cross-tenant admin/retention paths when RLS is enabled."""
    token = _rls_bypass.set(True)
    try:
        yield
    finally:
        _rls_bypass.reset(token)


def _resolve_rls_tenant(session: Session) -> str | None:
    tid = session.info.get("tenant_id") or _rls_tenant.get()
    if tid:
        return str(tid)
    from felix.context import try_get_context

    ctx = try_get_context()
    if ctx is not None:
        return ctx.auth.tenant_id
    return None


def _ensure_rls_listener() -> None:
    global _listener_installed
    if _listener_installed:
        return

    @event.listens_for(Session, "after_begin")
    def _after_begin(session: Session, transaction: object, connection: object) -> None:
        settings = get_settings()
        if not getattr(settings, "database_rls", False):
            return
        if _rls_bypass.get():
            connection.execute(text("SELECT set_config('app.rls_bypass', 'on', true)"))  # type: ignore[union-attr]
            return
        tid = _resolve_rls_tenant(session)
        if not tid:
            return
        connection.execute(  # type: ignore[union-attr]
            text("SELECT set_config('app.tenant_id', :t, true)"),
            {"t": tid},
        )

    _listener_installed = True


async def apply_tenant_rls(session: AsyncSession, settings: Settings, tenant_id: str) -> None:
    """Explicitly set RLS GUCs on an open session (also covered by after_begin)."""
    if not getattr(settings, "database_rls", False) or _use_memory(settings):
        return
    session.info["tenant_id"] = tenant_id
    if _rls_bypass.get():
        await session.execute(text("SELECT set_config('app.rls_bypass', 'on', true)"))
        return
    await session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})


# Engines created through get_engine, so dispose_engine can actually close them.
_ENGINES: list[AsyncEngine] = []

# Recycle before a pooler or cloud proxy drops an idle connection server-side. Without
# it, PgBouncer / RDS Proxy / Cloud SQL close connections the pool still believes are
# live, and pool_pre_ping pays a round trip to discover that on every checkout.
POOL_RECYCLE_SECONDS = 1800


def _pool_kwargs(settings: Settings) -> dict[str, object]:
    """Pool sizing for every engine this module builds.

    One function rather than two literal blocks: the second engine previously had no
    pool sizing at all, and when that was fixed it was fixed by copying the numbers --
    which is how two differently tuned pools against the same database happen.
    """
    return {
        "pool_pre_ping": settings.db_pool_pre_ping,
        "pool_size": settings.db_pool_size,
        "max_overflow": settings.db_max_overflow,
        "pool_recycle": POOL_RECYCLE_SECONDS,
        "pool_timeout": settings.db_pool_timeout_seconds,
    }


@lru_cache
def get_engine(database_url: str | None = None) -> AsyncEngine:
    _ensure_rls_listener()
    settings = get_settings()
    url = _normalize_url(database_url or settings.database_url)
    engine = create_async_engine(url, **_pool_kwargs(settings))  # type: ignore[arg-type]
    _ENGINES.append(engine)
    return engine


@lru_cache(maxsize=16)
def _factory_for(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """One sessionmaker per engine.

    Keyed on the engine rather than on settings: `Settings` is not hashable, and the
    engine is already the thing a factory is bound to. `dispose_engine` clears this
    alongside the engine cache, so a disposed engine cannot be kept alive by a factory
    still holding a reference to it.
    """
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


def get_session_factory(
    engine: AsyncEngine | None = None,
    *,
    settings: Settings | None = None,
) -> async_sessionmaker[AsyncSession]:
    _ensure_rls_listener()
    if engine is None:
        url = None if settings is None else settings.database_url
        engine = get_engine(url)
    return _factory_for(engine)


@asynccontextmanager
async def session_scope(
    factory: async_sessionmaker[AsyncSession] | None = None,
) -> AsyncIterator[AsyncSession]:
    factory = factory or get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def tenant_session(
    settings: Settings,
    tenant_id: str,
) -> AsyncIterator[AsyncSession]:
    """Open a session with RLS tenant GUC bound (no auto-commit)."""
    factory = get_session_factory(settings=settings)
    with rls_tenant(tenant_id):
        async with factory() as session:
            await apply_tenant_rls(session, settings, tenant_id)
            yield session


async def dispose_engine() -> None:
    """Close every engine this module created.

    This was `cache_clear()` plus a comment plus `pass` — so connections were never
    returned, and they lingered across Granian worker recycles. Keeping a list of the
    engines handed out is what makes disposal possible at all; `lru_cache` alone gives
    no way to reach them.
    """
    get_engine.cache_clear()
    # Or a factory keeps a disposed engine reachable.
    _factory_for.cache_clear()
    engines, _ENGINES[:] = list(_ENGINES), []
    for engine in engines:
        try:
            await engine.dispose()
        except Exception:
            logger.warning("engine dispose failed", exc_info=True)


def create_engine_from_settings(settings: Settings) -> AsyncEngine:
    """A second engine, previously with no pool sizing at all — so two differently
    tuned pools existed against the same database. Both now go through
    `_pool_kwargs`, so they cannot drift apart again."""
    _ensure_rls_listener()
    engine = create_async_engine(
        _normalize_url(settings.database_url),
        **_pool_kwargs(settings),  # type: ignore[arg-type]
    )
    _ENGINES.append(engine)
    return engine


__all__ = [
    "_use_memory",
    "apply_tenant_rls",
    "create_engine_from_settings",
    "dispose_engine",
    "get_engine",
    "get_session_factory",
    "rls_bypass",
    "rls_tenant",
    "session_scope",
    "tenant_session",
]
