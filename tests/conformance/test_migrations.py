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

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.conformance.conftest import downgrade_to_base, drop_everything, migrate_to_head, postgres_url

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("empty_database")]


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
    # The contract fixtures leave the schema at head for the next test; upgrading that is a
    # no-op, and this would pass without applying a revision. `empty_database` is what makes
    # "empty" true.
    assert await _scalar(url, "SELECT to_regclass('public.alembic_version')") is None, (
        "the database was not empty — this test would be upgrading a schema already at head"
    )
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


async def test_every_index_the_models_declare_exists_after_upgrade() -> None:
    """An `Index` in `db/models.py` that no revision creates exists only under `create_all`, which
    nothing in production runs: the query it was written for scans in production. `skill_file`'s
    digest index (0028) for `holds_imported_file` is the latest; this checks them all by name."""
    from felix.db.models import Base

    url = _url_or_skip()
    try:
        await migrate_to_head(url)
        declared = {
            (table.name, index.name) for table in Base.metadata.tables.values() for index in table.indexes
        }
        engine = create_async_engine(url, future=True)
        try:
            async with engine.connect() as conn:
                present = {
                    (r[0], r[1])
                    for r in (await conn.execute(text("SELECT tablename, indexname FROM pg_indexes"))).all()
                }
        finally:
            await engine.dispose()
        assert ("skill_file", "idx_skill_file_tenant_sha256") in declared
        assert declared <= present, (
            f"declared in the models, created by no revision: {sorted(declared - present)}"
        )
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


async def test_a_column_added_to_a_live_table_is_not_null_with_a_default() -> None:
    """`approvals.thread_id`, and the promise its migration makes about historical rows.

    `create_pending` always passes an explicit `""`, so every store-level test sees the Python
    kwarg default and none of them can see the DDL. Written `nullable=True` with no default,
    the whole conformance file stays green while every row predating the migration surfaces as
    `null` over `GET /approvals` — two contracts for one field, which is the thing a client
    author discovers in production. This asserts the column as the migration declares it.
    """
    url = _url_or_skip()
    try:
        await migrate_to_head(url)
        engine = create_async_engine(url, future=True)
        try:
            async with engine.connect() as conn:
                row = (
                    await conn.execute(
                        text(
                            "SELECT is_nullable, column_default FROM information_schema.columns "
                            "WHERE table_name = 'approvals' AND column_name = 'thread_id'"
                        )
                    )
                ).first()
        finally:
            await engine.dispose()

        assert row is not None, "0014 did not add approvals.thread_id"
        assert row[0] == "NO", f"thread_id is nullable, so an old row reads as null: {row[0]!r}"
        assert row[1] == "''::text", f"no server default, so the backfill-free claim fails: {row[1]!r}"
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


# A migration role the way managed Postgres hands one out: it owns the tables it migrates but is
# neither a superuser nor BYPASSRLS, so the forced tenant policy binds it. A literal password for a
# throwaway role on a test database, with no apostrophe (`CREATE ROLE` cannot take a parameter).
_MIGRATOR = "felix_conformance_migrator"
_MIGRATOR_PASSWORD = "conformance-migrator-not-a-secret"
_SKILL_TABLES = ("skill", "skill_version", "skill_file")


async def _execute(url: str, *statements: str) -> None:
    engine = create_async_engine(url, future=True, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            for statement in statements:
                await conn.execute(text(statement))
    finally:
        await engine.dispose()


async def _drop_migrator(url: str) -> None:
    await _execute(
        url,
        "DO $$ BEGIN "
        f"IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_MIGRATOR}') THEN "
        f"EXECUTE 'REASSIGN OWNED BY {_MIGRATOR} TO CURRENT_USER'; "
        f"EXECUTE 'DROP OWNED BY {_MIGRATOR}'; "
        "END IF; END $$",
        f"DROP ROLE IF EXISTS {_MIGRATOR}",
    )


async def _as_migrator(url: str) -> str:
    """A URL for a role that owns the skill tables and `alembic_version` and is bound by RLS.

    Plus every table a migration *after* `0033` alters, because reaching `0033` from head
    downgrades through each of them first: `fibers` (`0034_fiber_thread`)."""
    from sqlalchemy.engine import make_url

    await _drop_migrator(url)
    await _execute(
        url,
        f"CREATE ROLE {_MIGRATOR} LOGIN PASSWORD '{_MIGRATOR_PASSWORD}' NOSUPERUSER NOBYPASSRLS",
        f"GRANT USAGE, CREATE ON SCHEMA public TO {_MIGRATOR}",
        *(f'ALTER TABLE "{t}" OWNER TO {_MIGRATOR}' for t in (*_SKILL_TABLES, "fibers", "alembic_version")),
    )
    return (
        make_url(url)
        .set(username=_MIGRATOR, password=_MIGRATOR_PASSWORD)
        .render_as_string(hide_password=False)
    )


async def _primary_key(url: str, table: str) -> object:
    return await _scalar(
        url,
        "SELECT string_agg(a.attname, ',' ORDER BY k.ord) FROM pg_index i "
        "CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord) "
        "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum "
        f"WHERE i.indrelid = '{table}'::regclass AND i.indisprimary",
    )


async def _downgrade_past_0033(url: str) -> None:
    from alembic import command
    from felix.db.migrations import alembic_config

    await asyncio.to_thread(command.downgrade, alembic_config(url), "0032_skill_file_normalized")


async def test_skills_are_keyed_by_owner_and_a_personal_skill_blocks_the_downgrade() -> None:
    """`0033_skill_owner`: the three library tables key on `owner` beside the name, every row
    written before it is the org's (`''`), and the downgrade refuses while any of the three holds a
    personal row -- the old key cannot hold two owners' skills of one name, and one that fit would
    become the org's. Run as a role RLS binds: the compose superuser escapes the forced policy, so
    a guard counting without the bypass would pass here and wave every downgrade through on managed
    Postgres. A store test cannot see any of this: it never downgrades, and it always writes an owner.
    """
    url = _url_or_skip()
    try:
        await migrate_to_head(url)
        for table, key in (
            ("skill", "tenant_id,owner,name"),
            ("skill_version", "tenant_id,owner,name,version"),
            ("skill_file", "tenant_id,owner,name,version,path"),
        ):
            columns = await _primary_key(url, table)
            assert columns == key, f"{table} is keyed on {columns}"
            default = await _scalar(
                url,
                "SELECT column_default FROM information_schema.columns "
                f"WHERE table_name = '{table}' AND column_name = 'owner' AND is_nullable = 'NO'",
            )
            assert default == "''::text", f"{table}.owner is nullable or has no '' default: {default!r}"

        # Alice's skill, with no org skill of its name: the old key would take it without a collision.
        await _execute(
            url,
            "INSERT INTO skill (tenant_id, owner, name, live_version, created_at, updated_at) "
            "VALUES ('acme', 'iss|alice', 'notes', '0.1.0', 1, 1)",
            "INSERT INTO skill_version (tenant_id, owner, name, version, status, source, security_status, "
            "created_at) VALUES ('acme', 'iss|alice', 'notes', '0.1.0', 'published', 'agent', 'pass', 1)",
            "INSERT INTO skill_file (tenant_id, owner, name, version, path, sha256, size) "
            "VALUES ('acme', 'iss|alice', 'notes', '0.1.0', 'SKILL.md', 'a', 1)",
        )
        migrator = await _as_migrator(url)
        assert await _scalar(migrator, "SELECT count(*) FROM skill") == 0, (
            "the migrator role sees rows without the bypass, so this test cannot catch a guard missing it"
        )
        # Head, read rather than spelled: the refused downgrade rolls back whole, so the schema stays
        # wherever it started, and that is `0033` only until a later migration lands.
        head = await _scalar(url, "SELECT version_num FROM alembic_version")
        with pytest.raises(RuntimeError, match="personal libraries"):
            await _downgrade_past_0033(migrator)
        assert await _scalar(url, "SELECT version_num FROM alembic_version") == head

        # Versions removed and the skill row left behind still refuses: that row would re-key into
        # an org skill whose live version no longer exists.
        await _execute(
            url, "DELETE FROM skill_version WHERE owner <> ''", "DELETE FROM skill_file WHERE owner <> ''"
        )
        with pytest.raises(RuntimeError, match=r"\(1 in skill\)"):
            await _downgrade_past_0033(migrator)

        await _execute(url, "DELETE FROM skill WHERE owner <> ''")
        await _downgrade_past_0033(migrator)
        # `DROP COLUMN owner` would take the composite key with it and leave each table keyless,
        # so the old keys being back is the downgrade's work, not a side effect. Code before
        # 0033 upserts on `(tenant_id, name)` and fails on every save without it.
        for table, key in (
            ("skill", "tenant_id,name"),
            ("skill_version", "tenant_id,name,version"),
            ("skill_file", "tenant_id,name,version,path"),
        ):
            assert await _primary_key(url, table) == key, f"{table} lost its key on the way down"
        assert (
            await _scalar(
                url,
                "SELECT count(*) FROM information_schema.columns WHERE column_name = 'owner' "
                "AND table_name IN ('skill', 'skill_version', 'skill_file')",
            )
            == 0
        )
    finally:
        await drop_everything(url)
        await _drop_migrator(url)
