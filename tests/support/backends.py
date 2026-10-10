"""Reach the conformance Postgres and Redis, or skip — and fail instead when CI requires the backend."""

from __future__ import annotations

import asyncio
import os

import pytest
from felix.db.migrations import alembic_config

REDIS_URL_ENV = "FELIX_CONFORMANCE_REDIS_URL"
REQUIRE_REDIS_ENV = "FELIX_CONFORMANCE_REQUIRE_REDIS"


def redis_url_or_skip() -> str:
    url = os.environ.get(REDIS_URL_ENV)
    if not url:
        if os.environ.get(REQUIRE_REDIS_ENV):
            pytest.fail(f"{REQUIRE_REDIS_ENV} is set but {REDIS_URL_ENV} is not")
        pytest.skip(f"{REDIS_URL_ENV} unset — the cross-replica arm did not run")
    return url


PG_URL_ENV = "FELIX_CONFORMANCE_DATABASE_URL"
REQUIRE_ENV = "FELIX_CONFORMANCE_REQUIRE_POSTGRES"


def postgres_url() -> str | None:
    """The conformance database — never under xdist, where every worker would share it.

    The arm resets one schema per test (`ready_schema`), so two workers on one database truncate
    each other's rows mid-test. `make test` runs `-n auto`; the CI conformance job, and
    `make conformance`, run serially. Under xdist the arm skips, or fails where it is required.
    """
    if os.environ.get("PYTEST_XDIST_WORKER"):
        return None
    return os.environ.get(PG_URL_ENV) or None


def postgres_url_or_skip(what: str) -> str:
    """The conformance database, or a skip — or a failure where a skip would lie.

    Locally a skip is right; in CI `FELIX_CONFORMANCE_REQUIRE_POSTGRES` turns the missing
    database into a failure, because a silently skipped arm looks exactly like a pass.
    """
    url = postgres_url()
    if url:
        return url
    if os.environ.get(REQUIRE_ENV):
        pytest.fail(f"{REQUIRE_ENV} is set but {PG_URL_ENV} is not — the Postgres arm of {what} cannot run")
    pytest.skip(f"{PG_URL_ENV} unset — the Postgres arm of {what} did not run")


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

    await asyncio.to_thread(command.upgrade, alembic_config(url), "head")


async def downgrade_to_base(url: str) -> None:
    from alembic import command

    await asyncio.to_thread(command.downgrade, alembic_config(url), "base")


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


async def ready_schema(url: str) -> None:
    """A schema at head with no rows in it — migrating only when it is not already there.

    Each contract test used to apply every revision on setup and drop the schema on teardown:
    about 170 ms a test, paid again for each new revision and each new test, and roughly half
    of the job's wall time by revision 0020. The schema a test needs is the same every time, so
    it is built once, and each test after the first starts from `TRUNCATE` instead. What the
    rebuild bought — that every revision applies, that the DDL only a migration creates is
    present — `test_migrations.py` asserts on its own, from an empty database it drops to first.

    Reset at setup rather than at teardown, so a test that died mid-teardown cannot hand its
    rows to the next one; and decided by the stamped revision rather than a process flag, so a
    test that downgraded, dropped or half-migrated the schema is followed by a rebuild.
    """
    from felix.db.migrations import script_head
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(url, future=True)
    try:
        async with engine.begin() as conn:
            stamped = await conn.scalar(text("SELECT to_regclass('public.alembic_version')"))
            current = await conn.scalar(text("SELECT version_num FROM alembic_version")) if stamped else None
            if current is not None and current == script_head():
                tables = (
                    await conn.execute(
                        text(
                            "SELECT quote_ident(tablename) FROM pg_tables "
                            "WHERE schemaname = 'public' AND tablename <> 'alembic_version'"
                        )
                    )
                ).scalars()
                await conn.execute(text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"))
                return
    finally:
        await engine.dispose()
    await drop_everything(url)
    await migrate_to_head(url)
