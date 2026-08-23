"""Concurrent appends to one thread must not collide on `seq`.

`append_batch` computes `seq` as `max(seq) + 1` and the PK is
`(tenant_id, thread_id, seq)`. That read-modify-write is reachable by design: an SSE
stream, `/chat/steer`, `/chat/tool_result`, and `/chat/sessions/custom` all append to the
same thread concurrently. Two appends computed the same head, and one died with an
unhandled IntegrityError — an HTTP 500 with those events lost.

The full race needs a real Postgres (the suite runs on `memory://`, which has no such
path), so these tests pin the guard itself: that the advisory lock is taken on Postgres,
skipped elsewhere, and never turns a lock failure into a failed append.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.session.store import _lock_thread


class _FakeDialect:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeBind:
    def __init__(self, name: str) -> None:
        self.dialect = _FakeDialect(name)


class _FakeDb:
    def __init__(self, dialect: str = "postgresql", raises: bool = False) -> None:
        self.bind = _FakeBind(dialect)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._raises = raises

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> None:
        if self._raises:
            raise RuntimeError("advisory locks unavailable")
        self.calls.append((str(stmt), params or {}))


@pytest.mark.asyncio
async def test_lock_is_taken_on_postgres() -> None:
    db = _FakeDb("postgresql")
    await _lock_thread(db, "t1", "thread-a")
    assert len(db.calls) == 1
    sql, params = db.calls[0]
    assert "pg_advisory_xact_lock" in sql
    assert params["k"] == "felix:session:t1:thread-a"


@pytest.mark.asyncio
async def test_lock_key_is_per_thread() -> None:
    """Two threads must not serialize against each other."""
    a, b = _FakeDb(), _FakeDb()
    await _lock_thread(a, "t1", "thread-a")
    await _lock_thread(b, "t1", "thread-b")
    assert a.calls[0][1]["k"] != b.calls[0][1]["k"]


@pytest.mark.asyncio
async def test_lock_key_is_per_tenant() -> None:
    a, b = _FakeDb(), _FakeDb()
    await _lock_thread(a, "t1", "same")
    await _lock_thread(b, "t2", "same")
    assert a.calls[0][1]["k"] != b.calls[0][1]["k"]


@pytest.mark.asyncio
async def test_lock_is_skipped_on_other_backends() -> None:
    db = _FakeDb("sqlite")
    await _lock_thread(db, "t1", "thread-a")
    assert db.calls == []


@pytest.mark.asyncio
async def test_lock_failure_does_not_fail_the_append() -> None:
    """The unique PK still protects correctness; the lock only avoids the conflict."""
    db = _FakeDb("postgresql", raises=True)
    await _lock_thread(db, "t1", "thread-a")  # must not raise
