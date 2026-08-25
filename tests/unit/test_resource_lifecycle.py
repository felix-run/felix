"""Process-lifetime resources must be created once and released.

`build_object_store` was called inside `build_tenant_agent` — once per HTTP request —
and `S3ObjectStore.__aenter__` had no matching `__aexit__`, so every chat request leaked
an aiobotocore client and its connection pool until the process hit EMFILE. `_get_client`
also had no lock, so two concurrent first-requests each created a client and orphaned
one. And `dispose_engine` was `cache_clear()` plus a comment plus `pass`.
"""

from __future__ import annotations

import asyncio

import pytest
from felix.config import Settings
from felix.storage import (
    close_object_stores,
    get_object_store,
    reset_object_store_cache_for_tests,
)


def _settings(**kw: object) -> Settings:
    base: dict[str, object] = {
        "database_url": "memory://lifecycle",
        "object_store": "memory",
        "allow_insecure": True,
        "auth_mode": "none",
    }
    base.update(kw)
    return Settings(**base)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _clean() -> None:
    reset_object_store_cache_for_tests()


# --- object store cache ----------------------------------------------------------


def test_same_settings_reuse_one_store() -> None:
    """A fresh store per request is what leaked the client."""
    s = _settings()
    assert get_object_store(s) is get_object_store(s)


def test_distinct_settings_get_distinct_stores() -> None:
    a = get_object_store(_settings(object_store="memory"))
    b = get_object_store(_settings(object_store="fs", data_dir="/tmp/felix-lifecycle"))
    assert a is not b


def test_equal_settings_objects_share_a_store() -> None:
    """Keyed by configuration, not object identity — each request builds its own
    Settings instance."""
    assert get_object_store(_settings()) is get_object_store(_settings())


@pytest.mark.asyncio
async def test_close_releases_and_clears() -> None:
    closed: list[str] = []

    class _Store:
        async def close(self) -> None:
            closed.append("yes")

    from felix.storage import _STORE_CACHE

    _STORE_CACHE[("k",)] = _Store()  # type: ignore[assignment]
    await close_object_stores()
    assert closed == ["yes"]
    assert not _STORE_CACHE


@pytest.mark.asyncio
async def test_close_survives_a_failing_store() -> None:
    """One store that cannot close must not strand the others."""
    closed: list[str] = []

    class _Bad:
        async def close(self) -> None:
            raise RuntimeError("nope")

    class _Good:
        async def close(self) -> None:
            closed.append("good")

    from felix.storage import _STORE_CACHE

    _STORE_CACHE[("a",)] = _Bad()  # type: ignore[assignment]
    _STORE_CACHE[("b",)] = _Good()  # type: ignore[assignment]
    await close_object_stores()
    assert closed == ["good"]


@pytest.mark.asyncio
async def test_store_without_close_is_skipped() -> None:
    from felix.storage import _STORE_CACHE

    _STORE_CACHE[("k",)] = object()  # type: ignore[assignment]
    await close_object_stores()  # must not raise


# --- S3 client construction ------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_first_requests_create_one_client() -> None:
    """Both callers saw `_client is None` and both built one; the loser was orphaned
    with no reference left to close it."""
    from felix.storage.s3 import S3ObjectStore

    store = S3ObjectStore(_settings(object_store="s3", s3_bucket="b"))
    created = 0

    async def _fake_enter():
        nonlocal created
        await asyncio.sleep(0.01)  # widen the race window
        created += 1
        return object()

    class _CM:
        async def __aenter__(self):
            return await _fake_enter()

        async def __aexit__(self, *a):
            return False

    class _Session:
        def create_client(self, *a, **k):
            return _CM()

    import sys
    import types

    fake = types.ModuleType("aiobotocore.session")
    fake.get_session = lambda: _Session()  # type: ignore[attr-defined]
    sys.modules["aiobotocore.session"] = fake

    await asyncio.gather(*[store._get_client() for _ in range(8)])
    assert created == 1, f"created {created} clients for one store"


@pytest.mark.asyncio
async def test_close_is_idempotent() -> None:
    from felix.storage.s3 import S3ObjectStore

    store = S3ObjectStore(_settings(object_store="s3", s3_bucket="b"))
    await store.close()
    await store.close()  # must not raise on an unopened store


# --- engine disposal --------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispose_engine_actually_disposes() -> None:
    """It was `cache_clear()` plus a comment plus `pass`, so connections were never
    returned and lingered across worker recycles."""
    import felix.db.session as dbs

    disposed: list[str] = []

    class _Engine:
        async def dispose(self) -> None:
            disposed.append("yes")

    dbs._ENGINES.append(_Engine())  # type: ignore[arg-type]
    await dbs.dispose_engine()
    assert disposed == ["yes"]
    assert dbs._ENGINES == []


@pytest.mark.asyncio
async def test_dispose_survives_a_failing_engine() -> None:
    import felix.db.session as dbs

    disposed: list[str] = []

    class _Bad:
        async def dispose(self) -> None:
            raise RuntimeError("nope")

    class _Good:
        async def dispose(self) -> None:
            disposed.append("good")

    dbs._ENGINES.extend([_Bad(), _Good()])  # type: ignore[list-item]
    await dbs.dispose_engine()
    assert disposed == ["good"]
    assert dbs._ENGINES == []


def test_pool_is_tuned_for_a_pooler() -> None:
    """No pool_recycle meant PgBouncer / RDS Proxy / Cloud SQL dropped idle connections
    the pool still believed were live.

    The timeout moved from a module constant to `db_pool_timeout_seconds`, so the
    assertion follows it: what mattered was never where the number lived but that a
    checkout cannot block forever. Recycle stays a constant, because its value is a
    property of the pooler's own idle timeout rather than of this deployment.
    """
    from felix.config import Settings
    from felix.db.session import POOL_RECYCLE_SECONDS

    assert 0 < POOL_RECYCLE_SECONDS <= 3600
    assert Settings(database_url="memory://x").db_pool_timeout_seconds > 0
