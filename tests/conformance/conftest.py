"""Backend fixtures for the store conformance suite.

The in-memory arm always runs. The Postgres arm runs only when a database is reachable —
`FELIX_CONFORMANCE_DATABASE_URL`, which CI sets from its service container — and skips
otherwise. A skip here is a real gap in coverage, not a pass, so it says so.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

PG_URL_ENV = "FELIX_CONFORMANCE_DATABASE_URL"
REQUIRE_ENV = "FELIX_CONFORMANCE_REQUIRE_POSTGRES"


def postgres_url() -> str | None:
    return os.environ.get(PG_URL_ENV) or None


def _alembic_config(url: str):
    """An Alembic config pointed at `url`, whatever the ambient settings say.

    `migrations/env.py` reads `config.attributes["felix_url"]` before falling back to
    `FELIX_DATABASE_URL` — which `scripts/test.sh` pins to `memory://`, so without the
    override every migration here would target the in-memory URL and do nothing.
    """
    from pathlib import Path

    from alembic.config import Config

    root = Path(__file__).resolve().parents[2]
    cfg = Config(str(root / "alembic.ini"))
    cfg.attributes["felix_url"] = url
    return cfg


async def migrate_to_head(url: str) -> None:
    """Build the schema the way production does — by applying every revision.

    This suite used to call `Base.metadata.create_all`, which meant the revisions were
    never executed by CI and the DDL that lives only in a migration was never present:
    generated columns and non-btree indexes are deliberately kept out of the ORM
    (`session_events.content_tsv` is reached via `text()`), so `create_all` cannot
    produce them and anything depending on them was silently untested.

    Alembic drives its own event loop, so it runs in a worker thread.
    """
    from alembic import command

    await asyncio.to_thread(command.upgrade, _alembic_config(url), "head")


async def downgrade_to_base(url: str) -> None:
    from alembic import command

    await asyncio.to_thread(command.downgrade, _alembic_config(url), "base")


async def drop_everything(url: str) -> None:
    """Teardown that cannot fail on a broken downgrade.

    Deliberately not `downgrade base`: teardown should be boring. Whether the
    downgrades actually reverse is asserted by `test_migrations.py`, where a failure
    names itself instead of erroring every test in the suite.
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(url, future=True)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def memory_settings(request: pytest.FixtureRequest) -> AsyncIterator[Any]:
    """`Settings` pointed at the backend named by the parametrization.

    The memory store is reached through module-level functions that take `settings`
    rather than through a store object, so its contract is parametrized on settings
    rather than on an instance.
    """
    from felix.config import Settings
    from felix.db.session import dispose_engine
    from felix.memory import store as memory_store

    backend = request.param
    if backend == "memory":
        memory_store._memory_rows.clear()
        yield Settings(database_url="memory://conformance")
        memory_store._memory_rows.clear()
        return

    url = postgres_url()
    if not url:
        if os.environ.get(REQUIRE_ENV):
            pytest.fail(f"{REQUIRE_ENV} is set but {PG_URL_ENV} is not — the Postgres arm cannot run")
        pytest.skip(f"{PG_URL_ENV} unset — the Postgres arm of the contract did not run")

    await migrate_to_head(url)
    try:
        yield Settings(database_url=url)
    finally:
        # `get_engine` is lru_cached per URL, so the pooled connections outlive this
        # fixture and would hold locks on the schema the teardown is about to drop.
        await dispose_engine()
        await drop_everything(url)


@pytest_asyncio.fixture
async def store(request: pytest.FixtureRequest) -> AsyncIterator[Any]:
    """A session store for the backend named by the parametrization."""
    backend = request.param
    if backend == "memory":
        from felix.session.store import InMemorySessionStore

        # Same tenant as the Postgres arm below: the contract can only compare the
        # two backends on tenant-scoped behaviour if both are scoped to a tenant.
        yield InMemorySessionStore(tenant_id="conformance")
        return

    url = postgres_url()
    if not url:
        # Locally a skip is the right answer — not everyone has a database running. In CI
        # it is not: a silently skipped arm is a coverage gap that looks exactly like a
        # pass, which is the failure mode this whole suite exists to remove.
        if os.environ.get(REQUIRE_ENV):
            pytest.fail(f"{REQUIRE_ENV} is set but {PG_URL_ENV} is not — the Postgres arm cannot run")
        pytest.skip(f"{PG_URL_ENV} unset — the Postgres arm of the contract did not run")

    from felix.session.store import PostgresSessionStore
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    await migrate_to_head(url)
    engine = create_async_engine(url, future=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield PostgresSessionStore(factory, tenant_id="conformance")
    finally:
        await drop_everything(url)
        await engine.dispose()
