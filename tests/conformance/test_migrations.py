"""The migrations themselves, applied to a real database.

Nothing in this repo executed a revision before: the conformance suite built its
schema with `Base.metadata.create_all`, and `grep -rln alembic tests/` was empty. So a
revision that failed to apply, or that drifted from the models, shipped green.

The gap matters most for DDL that has no ORM representation. Generated columns and
non-btree indexes are declared only inside a migration's `op.execute` and reached from
Python via `text()` — `session_events.content_tsv` is the existing example, and the
planned memory work adds a `tsvector` column and an HNSW index the same way.
`create_all` cannot produce any of it, so tests that depended on it would have failed
against the very database CI provides.

Postgres-only by construction: these assert against `pg_catalog`.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.conformance.conftest import downgrade_to_base, drop_everything, migrate_to_head, postgres_url

pytestmark = pytest.mark.asyncio


def _url_or_skip() -> str:
    url = postgres_url()
    if not url:
        import os

        if os.environ.get("FELIX_CONFORMANCE_REQUIRE_POSTGRES"):
            pytest.fail("FELIX_CONFORMANCE_REQUIRE_POSTGRES is set but no database URL was given")
        pytest.skip("FELIX_CONFORMANCE_DATABASE_URL unset — the migration arm did not run")
    return url


async def _scalar(url: str, sql: str) -> object:
    engine = create_async_engine(url, future=True)
    try:
        async with engine.connect() as conn:
            return (await conn.execute(text(sql))).scalar()
    finally:
        await engine.dispose()


async def test_upgrade_head_applies_every_revision() -> None:
    """`alembic upgrade head` on an empty database, which CI never did before."""
    url = _url_or_skip()
    try:
        await migrate_to_head(url)
        stamped = await _scalar(url, "SELECT count(*) FROM alembic_version")
        assert stamped == 1, "alembic did not stamp a single head revision"
        tables = await _scalar(
            url,
            "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'",
        )
        assert isinstance(tables, int) and tables > 5, f"suspiciously few tables: {tables}"
    finally:
        await drop_everything(url)


async def test_migration_only_ddl_exists_after_upgrade() -> None:
    """The DDL `create_all` structurally cannot produce.

    `content_tsv` is a generated column created by `0005_session_fts` and absent from
    `db/models.py` on purpose, so `create_all` cannot produce it. That is the concrete
    thing the old fixture could not have built, and the reason FTS was untestable
    against the database CI already provided.
    """
    url = _url_or_skip()
    try:
        await migrate_to_head(url)

        generated = await _scalar(
            url,
            "SELECT is_generated FROM information_schema.columns "
            "WHERE table_name = 'session_events' AND column_name = 'content_tsv'",
        )
        assert generated == "ALWAYS", f"content_tsv missing or not generated: {generated!r}"

        index = await _scalar(
            url,
            "SELECT indexdef FROM pg_indexes "
            "WHERE tablename = 'session_events' AND indexname = 'idx_session_events_content_tsv'",
        )
        assert index is not None and "gin" in str(index).lower(), (
            f"expected a GIN index on content_tsv, got: {index!r}"
        )
    finally:
        await drop_everything(url)


async def test_downgrades_reverse_cleanly() -> None:
    """Every revision reverses, and the schema re-applies afterwards.

    A downgrade nobody runs is a downgrade nobody knows is broken — and it is the only
    rollback path a bad deploy has.
    """
    url = _url_or_skip()
    try:
        await migrate_to_head(url)
        await downgrade_to_base(url)

        left = await _scalar(
            url,
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name <> 'alembic_version'",
        )
        assert left == 0, f"{left} table(s) survived a full downgrade"

        await migrate_to_head(url)
        assert await _scalar(url, "SELECT count(*) FROM alembic_version") == 1
    finally:
        await drop_everything(url)
