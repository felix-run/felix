"""Felix behind a transaction-mode connection pooler.

`WORKERS × (POOL_SIZE + MAX_OVERFLOW)` is the connection ceiling, and at four workers
on today's defaults that is 120 against a stock Postgres `max_connections` of 100. The
standard answer is PgBouncer in transaction mode — and Felix could not use it.

psycopg3 auto-prepares a statement after five executions. Under transaction pooling the
sixth lands on a different server connection where that statement was never created.
Measured against PgBouncer 1.25 with `max_prepared_statements=0`: the append path failed
on exactly the sixth append with `InFailedSqlTransaction`, and completed forty with
preparation disabled.

Two things were already safe, which is why this is the *only* blocker:
`pg_advisory_xact_lock` is transaction-scoped, and RLS uses `set_config(..., true)`,
which is `SET LOCAL`. Both live and die with the transaction.

CI has no PgBouncer, so these assert the wiring that the live test confirmed. The
integration evidence lives in the PR.
"""

from __future__ import annotations

from felix.config import Settings
from felix.db import session as db_session

PG = "postgresql+psycopg://u:p@localhost:5432/db"


def _settings(**kw: object) -> Settings:
    return Settings(database_url=PG, **kw)


def test_preparation_is_on_by_default() -> None:
    """The default suits a direct Postgres, which is what `make up` gives you."""
    assert _settings().db_prepared_statements is True
    assert "prepare_threshold" not in db_session._connect_args(_settings(), PG)


def test_disabling_it_passes_prepare_threshold_none() -> None:
    """`None` disables auto-preparation. `0` would prepare *everything* immediately,
    which is the opposite of what a pooler needs."""
    args = db_session._connect_args(_settings(db_prepared_statements=False), PG)
    assert "prepare_threshold" in args and args["prepare_threshold"] is None


def test_the_option_is_not_passed_to_a_driver_that_has_no_such_option() -> None:
    """`prepare_threshold` is a psycopg option. Passing it to another driver is a
    connection-time error rather than a no-op, so the URL is checked rather than
    assumed — `memory://` and any future backend must stay unaffected."""
    disabled = _settings(db_prepared_statements=False)
    for url in ("memory://x", "sqlite+aiosqlite:///tmp/x.db", "postgresql+asyncpg://u:p@h/db"):
        assert db_session._connect_args(disabled, url) == {}, url


def test_the_engine_kwargs_carry_the_option_to_every_engine() -> None:
    """Reaching the engine is the half that matters; `_connect_args` returning the right
    dict is worth nothing if a constructor does not pass it.

    The structural half of this — that no engine configures itself — lives in
    `test_db_pool_settings.py:test_no_engine_configures_itself`, which asserts it for
    every engine rather than for exactly two. The version here counted them, so a third
    engine added for a good reason would have failed a test whose message read "expected
    two engine constructors".
    """
    off = db_session._engine_kwargs(_settings(db_prepared_statements=False), PG)["connect_args"]
    on = db_session._engine_kwargs(_settings(db_prepared_statements=True), PG)["connect_args"]
    assert "prepare_threshold" in off and off["prepare_threshold"] is None  # type: ignore[index]
    assert "prepare_threshold" not in on  # type: ignore[operator]
