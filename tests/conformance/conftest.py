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
from felix.db.migrations import alembic_config

PG_URL_ENV = "FELIX_CONFORMANCE_DATABASE_URL"
REQUIRE_ENV = "FELIX_CONFORMANCE_REQUIRE_POSTGRES"


def postgres_url() -> str | None:
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


RLS_ROLE = "felix_conformance_rls"
# Interpolated into `CREATE ROLE` as a SQL literal, because DDL takes no bind parameters —
# so it must contain no apostrophe. A throwaway value for a throwaway role on a test database.
RLS_PASSWORD = "conformance-rls-not-a-secret"


async def _grant_restricted_role(admin_url: str) -> None:
    """Create a role the tenant policy actually applies to, and let it use the schema.

    Every other arm connects as the database owner, which is a superuser in CI and in the
    bundled compose image. Migration 0006 applies FORCE, but a superuser bypasses even that —
    so the policy is unreachable from the rest of this suite and everything it protects is
    asserted only by reading the SQL. This role is `NOSUPERUSER NOBYPASSRLS` and owns nothing,
    which is the shape of a managed-Postgres application role.
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(admin_url, future=True, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            # `DROP OWNED BY` first, so setup is idempotent however the previous run died.
            # The teardown's swallow self-heals only when `drop_everything` ran after it; if
            # the process was killed between the grant and the `try:`, both the role and the
            # tables survive and `DROP ROLE` then fails with DependentObjectsStillExist —
            # wedging every later run with eight setup errors that look nothing like the cause.
            # CI never sees it (fresh container per job); a developer would, until they cleaned
            # the cluster by hand. `DROP OWNED BY` has no IF EXISTS, hence the guard.
            await conn.execute(
                text(
                    "DO $$ BEGIN "
                    f"IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{RLS_ROLE}') THEN "
                    f"EXECUTE 'DROP OWNED BY {RLS_ROLE}'; "
                    "END IF; END $$"
                )
            )
            await conn.execute(text(f"DROP ROLE IF EXISTS {RLS_ROLE}"))
            # A literal, not a bind parameter: `CREATE ROLE` is utility DDL and the extended
            # query protocol cannot parameterise it — psycopg reports `syntax error at or near
            # "$1"`. The password is a fixed constant in this file, so there is no injection
            # surface.
            await conn.execute(
                text(f"CREATE ROLE {RLS_ROLE} LOGIN PASSWORD '{RLS_PASSWORD}' NOSUPERUSER NOBYPASSRLS")
            )
            # After `migrate_to_head`, so every migrated table is covered. Anything created
            # *later* is not, and the failure is a bare `permission denied for table ...` that
            # looks nothing like a policy problem.
            await conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {RLS_ROLE}"))
            await conn.execute(
                text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {RLS_ROLE}")
            )
            await conn.execute(text(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {RLS_ROLE}"))
    finally:
        await engine.dispose()


async def _drop_restricted_role(admin_url: str) -> None:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(admin_url, future=True, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(text(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {RLS_ROLE}"))
            await conn.execute(text(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {RLS_ROLE}"))
            await conn.execute(text(f"REVOKE USAGE ON SCHEMA public FROM {RLS_ROLE}"))
            await conn.execute(text(f"DROP ROLE IF EXISTS {RLS_ROLE}"))
    except Exception:  # pragma: no cover — teardown must not mask a test failure
        # Tolerable because it self-heals: the grants are what a `DROP ROLE` depends on, and
        # `drop_everything` removes the tables carrying them straight after, so the next run's
        # `DROP ROLE IF EXISTS` succeeds. The cost of a silent failure here is a role left in
        # the cluster until then, not a wedged suite.
        pass
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def rls_settings(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Any]:
    """`Settings` for a connection the tenant policy is actually enforced on.

    Postgres only, and deliberately so: `memory://` has no policy to enforce, and a memory arm
    here would assert that nothing happens. What this covers is the configuration every other
    arm cannot reach — `database_rls=True` on a role that cannot bypass — which is where the
    difference between "the query layer scopes this" and "the database scopes this" shows up.
    """
    import felix.db.session as db_session
    from felix.config import Settings
    from felix.db.session import dispose_engine
    from sqlalchemy.engine import make_url

    admin_url = postgres_url_or_skip("the RLS enforcement contract")
    await migrate_to_head(admin_url)
    await _grant_restricted_role(admin_url)

    # `render_as_string(hide_password=False)`, never `str(...)`: `URL.__str__` masks the
    # password as `***`, so the role authenticates with the literal string `***` and every test
    # fails on `password authentication failed` rather than on anything about the policy. It
    # also URL-encodes correctly, which hand-assembling the string would not.
    restricted = (
        make_url(admin_url)
        .set(username=RLS_ROLE, password=RLS_PASSWORD)
        .render_as_string(hide_password=False)
    )
    settings = Settings(database_url=restricted, database_rls=True)
    # `_rls_after_begin` reads the *process-global* `get_settings()`, not the settings the store
    # was handed, and neither `scripts/test.sh` nor CI sets `FELIX_DATABASE_RLS`. Without this
    # every transaction declares `app.rls_bypass='on'` and the policy never filters anything —
    # the fixture would connect as a restricted role and prove nothing.
    # `tests/unit/test_rls_gucs.py` patches the same seam.
    monkeypatch.setattr(db_session, "get_settings", lambda: settings)
    try:
        yield settings
    finally:
        await dispose_engine()
        await _drop_restricted_role(admin_url)
        await drop_everything(admin_url)


@pytest_asyncio.fixture
async def store_settings(request: pytest.FixtureRequest) -> AsyncIterator[Any]:
    """`Settings` for a module-function store, on the backend named by the parametrization.

    The seams these contracts cover are module-level functions taking `settings` rather than
    store objects, so the contract is parametrized on settings the way `memory_settings` and
    `usage_settings` already are. This one is generic: adding a seam means a contract file,
    not another fixture.
    """
    from felix.config import Settings
    from felix.db.session import dispose_engine

    # Nothing to clear here: the autouse fixture in `tests/conftest.py` already resets every
    # in-memory twin these contracts touch, for every test in the repo. Duplicating it would
    # suggest it does not.
    #
    # `dispose_engine` below is process-global and disposes engines other fixtures made, so
    # this is safe only while the suite runs serially. There is no xdist today; if that
    # changes, this fixture needs its own engine rather than the shared cache.
    backend = request.param
    if backend == "memory":
        yield Settings(database_url="memory://conformance")
        return

    url = postgres_url_or_skip("the store contract")
    await migrate_to_head(url)
    try:
        yield Settings(database_url=url)
    finally:
        # `get_engine` is lru_cached per URL, so pooled connections outlive this fixture and
        # would hold locks on the schema the teardown drops.
        await dispose_engine()
        await drop_everything(url)


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
async def usage_settings(request: pytest.FixtureRequest) -> AsyncIterator[Any]:
    """`Settings` pointed at the backend named by the parametrization, for the usage store.

    The usage store is module-level functions over a process buffer plus one of two
    sinks; the buffer is drained around each test so an arm never inherits another's rows.
    """
    from felix.config import Settings
    from felix.db.session import dispose_engine
    from felix.usage import store as usage_store

    usage_store.pending_buffer().reset_for_tests()
    usage_store.clear_memory()
    backend = request.param
    if backend == "memory":
        yield Settings(database_url="memory://conformance")
        usage_store.clear_memory()
        return

    url = postgres_url()
    if not url:
        if os.environ.get(REQUIRE_ENV):
            pytest.fail(f"{REQUIRE_ENV} is set but {PG_URL_ENV} is not — the Postgres arm cannot run")
        pytest.skip(f"{PG_URL_ENV} unset — the Postgres arm of the usage contract did not run")

    await migrate_to_head(url)
    try:
        yield Settings(database_url=url)
    finally:
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


@pytest_asyncio.fixture
async def document_settings(request: pytest.FixtureRequest) -> AsyncIterator[Any]:
    """`Settings` pointed at the backend named by the parametrization.

    Same shape as `memory_settings`, and for the same reason: the corpus store exists twice —
    a dict walk for `memory://` and SQL with a generated tsvector plus pgvector for Postgres —
    and only running one contract against both keeps the copies honest. The in-memory arm's
    cosine similarity and the Postgres arm's `<=>` operator are two different rankers that
    have to agree about what a hit *is*.
    """
    from felix.config import Settings
    from felix.db.session import dispose_engine
    from felix.documents import store as doc_store

    backend = request.param
    if backend == "memory":
        doc_store.reset_documents_for_tests()
        yield Settings(database_url="memory://conformance")
        doc_store.reset_documents_for_tests()
        return

    url = postgres_url()
    if not url:
        if os.environ.get(REQUIRE_ENV):
            pytest.fail(f"{REQUIRE_ENV} is set but {PG_URL_ENV} is not — the Postgres arm cannot run")
        pytest.skip(f"{PG_URL_ENV} unset — the Postgres arm of the corpus contract did not run")

    await migrate_to_head(url)
    try:
        yield Settings(database_url=url)
    finally:
        # Teardown, like the sibling fixtures. Without it the Postgres arm never reset while
        # the memory arm reset around every test, so the two were no longer running the same
        # contract from the same state — which is the premise. It passed only because every
        # test here ingests the same (source, title) and so overwrites the same doc_id; the
        # first test with a second title would have made the arm order-dependent.
        await dispose_engine()
        await drop_everything(url)


@pytest_asyncio.fixture
async def retention_settings(request: pytest.FixtureRequest) -> AsyncIterator[Any]:
    """`Settings` for the retention contract, with every swept store empty on both sides.

    The sweep reaches seven tables through seven module-level stores; each in-memory
    twin is cleared here so an arm never inherits another test's rows.
    """
    from felix.a2a import tasks as a2a_store
    from felix.audit import store as audit_store
    from felix.config import Settings
    from felix.db.session import dispose_engine
    from felix.durability import fibers as fiber_store
    from felix.manifests import store as manifest_store
    from felix.memory import store as memory_store
    from felix.plans import store as plans_store
    from felix.session import store as session_store
    from felix.session import thread_state, tree
    from felix.usage import store as usage_store

    def clear() -> None:
        audit_store.pending_buffer().reset_for_tests()
        audit_store._memory_events.clear()
        usage_store.pending_buffer().reset_for_tests()
        usage_store.clear_memory()
        fiber_store.reset_memory_fibers()
        a2a_store.clear_tasks()
        plans_store._memory_plans.clear()
        memory_store._memory_rows.clear()
        session_store._memory_session_stores.clear()
        thread_state._meta_by_thread.clear()
        tree._leaf_by_thread.clear()
        manifest_store.reset_memory_store()

    clear()
    backend = request.param
    if backend == "memory":
        yield Settings(database_url="memory://conformance")
        clear()
        return

    url = postgres_url()
    if not url:
        if os.environ.get(REQUIRE_ENV):
            pytest.fail(f"{REQUIRE_ENV} is set but {PG_URL_ENV} is not — the Postgres arm cannot run")
        pytest.skip(f"{PG_URL_ENV} unset — the Postgres arm of the retention contract did not run")

    await migrate_to_head(url)
    try:
        yield Settings(database_url=url)
    finally:
        await dispose_engine()
        await drop_everything(url)
        clear()


@pytest_asyncio.fixture
async def fiber_settings(request: pytest.FixtureRequest) -> AsyncIterator[Any]:
    """`Settings` for the fiber store contract, both arms."""
    from felix.config import Settings
    from felix.db.session import dispose_engine
    from felix.durability import fibers as fiber_store

    fiber_store.reset_memory_fibers()
    backend = request.param
    if backend == "memory":
        yield Settings(database_url="memory://conformance")
        fiber_store.reset_memory_fibers()
        return

    url = postgres_url()
    if not url:
        if os.environ.get(REQUIRE_ENV):
            pytest.fail(f"{REQUIRE_ENV} is set but {PG_URL_ENV} is not — the Postgres arm cannot run")
        pytest.skip(f"{PG_URL_ENV} unset — the Postgres arm of the fiber contract did not run")

    await migrate_to_head(url)
    try:
        yield Settings(database_url=url)
    finally:
        await dispose_engine()
        await drop_everything(url)
