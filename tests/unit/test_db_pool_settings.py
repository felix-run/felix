"""The connection pool is configuration, not a constant in two places.

Fifteen connections per worker used to be a hard ceiling — `pool_size=5,
max_overflow=10` written literally into two engine constructors. Past it, requests
queue for the pool timeout and then fail, and the session-event append path and the
resume poll are both connection-hungry, so the ceiling arrives sooner than the number
suggests. Nobody could raise it without editing the source.

`test_invariants.py` already checks that every `FELIX_` field reaches `.env.example`.
What it cannot check is that the values are *used* — a setting that exists and is
ignored is worse than a constant, because it looks adjustable.
"""

from __future__ import annotations

import pytest
from felix.config import Settings
from felix.db import session as db_session

PG = "postgresql+psycopg://u:p@localhost:5432/db"


def _settings(**kw: object) -> Settings:
    return Settings(database_url=PG, **kw)


def _kwargs(**kw: object) -> dict[str, object]:
    return db_session._engine_kwargs(_settings(**kw), PG)


def test_the_engine_kwargs_come_from_settings() -> None:
    kwargs = _kwargs(
        db_pool_size=7,
        db_max_overflow=13,
        db_pool_timeout_seconds=11.5,
        db_pool_pre_ping=False,
    )
    assert kwargs["pool_size"] == 7
    assert kwargs["max_overflow"] == 13
    assert kwargs["pool_timeout"] == 11.5
    assert kwargs["pool_pre_ping"] is False


def test_the_defaults_raise_the_old_ceiling() -> None:
    """The point of the change, not just its mechanism."""
    kwargs = _kwargs()
    ceiling = int(kwargs["pool_size"]) + int(kwargs["max_overflow"])
    assert ceiling == 30, f"expected the documented default ceiling, got {ceiling}"
    assert ceiling > 15, "the old hardcoded ceiling"


def test_recycle_stays_a_constant() -> None:
    """Deliberately not a setting: it exists to beat a pooler's own idle timeout, and
    the value is a property of PgBouncer/RDS Proxy defaults rather than of this
    deployment."""
    assert _kwargs()["pool_recycle"] == db_session.POOL_RECYCLE_SECONDS


def test_no_engine_configures_itself() -> None:
    """The second engine once had no pool sizing at all, and the fix copied the literals
    across. Two differently tuned pools against one database is the failure this asserts
    against — so it checks that no constructor carries its own.

    `connect_args` is in the list because it was briefly a *separate* helper, which put
    every engine back in the position of having to remember two things independently.
    Deliberately not asserting how many engines exist: a third added for a good reason
    should inherit this check, not fail it.
    """
    import ast
    import inspect

    own = {"pool_size", "max_overflow", "pool_pre_ping", "pool_timeout", "connect_args"}
    tree = ast.parse(inspect.getsource(db_session))
    seen = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or getattr(node.func, "id", None) != "create_async_engine":
            continue
        seen += 1
        literals = {kw.arg for kw in node.keywords if kw.arg is not None} & own
        assert not literals, (
            f"create_async_engine at line {node.lineno} configures itself ({sorted(literals)}); "
            "go through _engine_kwargs so engines cannot drift apart"
        )
    assert seen, "found no engine constructors — has this module been restructured?"


def test_the_session_factory_is_reused_per_engine() -> None:
    """It was rebuilt on every store operation."""

    class _FakeEngine:
        pass

    engine = _FakeEngine()
    first = db_session.get_session_factory(engine)  # type: ignore[arg-type]
    second = db_session.get_session_factory(engine)  # type: ignore[arg-type]
    assert first is second


@pytest.mark.asyncio
async def test_disposal_clears_the_factory_cache() -> None:
    """Otherwise a cached factory keeps a disposed engine reachable."""

    class _FakeEngine:
        pass

    engine = _FakeEngine()
    first = db_session.get_session_factory(engine)  # type: ignore[arg-type]
    await db_session.dispose_engine()
    second = db_session.get_session_factory(engine)  # type: ignore[arg-type]
    assert first is not second, "dispose_engine left the sessionmaker cached"


def test_workers_is_a_setting_not_an_environ_read() -> None:
    """`main.py` read FELIX_WORKERS with a bare `os.environ.get` — invisible to
    `felix doctor`, absent from .env.example, and unvalidated."""
    import inspect

    from felix_api import main

    assert _settings(workers=4).workers == 4
    with pytest.raises(ValueError):
        _settings(workers=0)
    assert "os.environ" not in inspect.getsource(main), "main.py still reads the env directly"
