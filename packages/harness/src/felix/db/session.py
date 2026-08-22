"""Async SQLAlchemy engine / session factory from Settings."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from felix.config import Settings, get_settings


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


@lru_cache
def get_engine(database_url: str | None = None) -> AsyncEngine:
    settings = get_settings()
    url = _normalize_url(database_url or settings.database_url)
    return create_async_engine(
        url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
    )


def get_session_factory(
    engine: AsyncEngine | None = None,
    *,
    settings: Settings | None = None,
) -> async_sessionmaker[AsyncSession]:
    if engine is None:
        url = None if settings is None else settings.database_url
        engine = get_engine(url)
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


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


async def dispose_engine() -> None:
    get_engine.cache_clear()
    # Recreate briefly to dispose any cached engine reference is tricky with lru_cache;
    # callers that hold an engine should call engine.dispose() directly.
    pass


def create_engine_from_settings(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        _normalize_url(settings.database_url),
        pool_pre_ping=True,
    )


__all__ = [
    "_use_memory",
    "create_engine_from_settings",
    "dispose_engine",
    "get_engine",
    "get_session_factory",
    "session_scope",
]
