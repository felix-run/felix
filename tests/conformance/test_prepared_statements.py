"""`FELIX_DB_PREPARED_STATEMENTS` reaches the wire, not just the kwargs dict.

`_engine_kwargs` returning `{"prepare_threshold": None}` is a pure-function assertion,
and `tests/unit/test_pooler_compatibility.py` makes it. What that cannot show is that
the option survives into a live connection and actually stops psycopg preparing --
which is the only thing the setting is for.

The failure it guards is a late one. psycopg3 auto-prepares a statement on its sixth
execution; under transaction pooling the sixth lands on a different server connection
where that statement was never created, and the query fails. Five identical queries
succeed first. A test that runs one query proves nothing about it, so this runs seven.

Needs a real database (`FELIX_CONFORMANCE_DATABASE_URL`); CI sets
`FELIX_CONFORMANCE_REQUIRE_POSTGRES` so a missing one fails instead of skipping.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import text

from tests.conformance.conftest import PG_URL_ENV, REQUIRE_ENV, postgres_url

# One more than psycopg3's default `prepare_threshold` of 5, so the run crosses the
# point where preparation kicks in rather than stopping just short of it.
EXECUTIONS = 7


def _url() -> str:
    url = postgres_url()
    if url:
        return url
    if os.environ.get(REQUIRE_ENV):
        pytest.fail(f"{REQUIRE_ENV} is set but {PG_URL_ENV} is not")
    pytest.skip(f"{PG_URL_ENV} unset — the prepared-statement contract did not run")


async def _prepared_after_repeated_queries(*, enabled: bool) -> int:
    """Run one parameterised query `EXECUTIONS` times and count what got prepared.

    All on a single connection: `pg_prepared_statements` reports the current session's,
    and a query that lands on a fresh connection each time would report zero whatever
    the setting says -- passing for the wrong reason in exactly the direction this test
    is meant to detect.
    """
    from felix.config import Settings
    from felix.db.session import _engine_kwargs
    from sqlalchemy.ext.asyncio import create_async_engine

    url = _url()
    settings = Settings(database_url=url, db_prepared_statements=enabled)
    engine = create_async_engine(url, **_engine_kwargs(settings, url))  # type: ignore[arg-type]
    try:
        async with engine.connect() as conn:
            for i in range(EXECUTIONS):
                # `cast(:n as int)`, not `:n::int` -- `::` collides with SQLAlchemy's
                # own bind-parameter syntax and never reaches Postgres.
                result = await conn.execute(text("select cast(:n as int) as n"), {"n": i})
                assert result.scalar() == i
            count = await conn.execute(text("select count(*) from pg_prepared_statements"))
            return int(count.scalar() or 0)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_disabling_prepared_statements_actually_stops_them() -> None:
    """The pooler-compatible setting, verified by asking Postgres rather than psycopg."""
    assert await _prepared_after_repeated_queries(enabled=False) == 0


@pytest.mark.asyncio
async def test_the_default_does_prepare_so_the_test_above_means_something() -> None:
    """The control.

    Without it, `== 0` above passes against a driver that never prepares anything, a
    query shape psycopg declines to prepare, or an execution count that stopped short of
    the threshold -- none of which would say a word about the setting.
    """
    assert await _prepared_after_repeated_queries(enabled=True) > 0
