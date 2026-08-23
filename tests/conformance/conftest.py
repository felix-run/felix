"""Backend fixtures for the store conformance suite.

The in-memory arm always runs. The Postgres arm runs only when a database is reachable —
`FELIX_CONFORMANCE_DATABASE_URL`, which CI sets from its service container — and skips
otherwise. A skip here is a real gap in coverage, not a pass, so it says so.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

PG_URL_ENV = "FELIX_CONFORMANCE_DATABASE_URL"
REQUIRE_ENV = "FELIX_CONFORMANCE_REQUIRE_POSTGRES"


def postgres_url() -> str | None:
    return os.environ.get(PG_URL_ENV) or None


@pytest_asyncio.fixture
async def store(request: pytest.FixtureRequest) -> AsyncIterator[Any]:
    """A session store for the backend named by the parametrization."""
    backend = request.param
    if backend == "memory":
        from felix.session.store import InMemorySessionStore

        yield InMemorySessionStore()
        return

    url = postgres_url()
    if not url:
        # Locally a skip is the right answer — not everyone has a database running. In CI
        # it is not: a silently skipped arm is a coverage gap that looks exactly like a
        # pass, which is the failure mode this whole suite exists to remove.
        if os.environ.get(REQUIRE_ENV):
            pytest.fail(f"{REQUIRE_ENV} is set but {PG_URL_ENV} is not — the Postgres arm cannot run")
        pytest.skip(f"{PG_URL_ENV} unset — the Postgres arm of the contract did not run")

    from felix.db.models import Base
    from felix.session.store import PostgresSessionStore
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(url, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield PostgresSessionStore(factory, tenant_id="conformance")
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()
