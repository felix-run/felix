"""Every statement a flush sends fits Postgres's bind-parameter limit -- checked without a database.

The conformance tests prove it against a real Postgres, but they skip without one, so outside the
`conformance` job nothing guarded it. Here each writer runs against a session that only records
what it is asked to execute; every statement is compiled for the postgresql dialect and its
parameters counted. A writer that went back to one INSERT per tenant's batch fails on the count.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.config import Settings
from sqlalchemy.dialects import postgresql

PG_MAX_BIND_PARAMETERS = 65_535
SETTINGS = Settings(database_url="postgresql+psycopg://flush:test@127.0.0.1:1/none")


class _Recording:
    """A session factory whose sessions record each statement and commit nothing."""

    def __init__(self) -> None:
        self.statements: list[Any] = []

    def __call__(self) -> _Recording:
        return self

    async def __aenter__(self) -> _Recording:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def execute(self, stmt: Any, *a: Any, **kw: Any) -> Any:
        self.statements.append(stmt)

    async def commit(self) -> None:
        return None


def _bind_counts(statements: list[Any]) -> list[int]:
    return [len(stmt.compile(dialect=postgresql.dialect()).params) for stmt in statements]


@pytest.fixture
def recording(monkeypatch: pytest.MonkeyPatch) -> _Recording:
    from felix.audit import store as audit_store
    from felix.db import session as db_session
    from felix.usage import store as usage_store

    rec = _Recording()

    async def no_rls(*a: Any, **kw: Any) -> None:
        return None

    monkeypatch.setattr(db_session, "apply_tenant_rls", no_rls)
    for module in (audit_store, usage_store):
        monkeypatch.setattr(module, "get_session_factory", lambda **kw: rec)
    return rec


async def test_a_full_audit_buffer_is_written_in_statements_postgres_accepts(recording: _Recording) -> None:
    from felix.audit import store as audit_store
    from felix.buffers import DEFAULT_MAX_PENDING

    batch = [
        {"tenant_id": "acme", "id": f"e{i}", "ts": i, "event_type": "tool_call", "payload_json": {}}
        for i in range(DEFAULT_MAX_PENDING)
    ]
    await audit_store._write_batch(SETTINGS, batch)
    counts = _bind_counts(recording.statements)
    assert sum(counts) >= DEFAULT_MAX_PENDING, "the batch was not written at all"
    assert max(counts) <= PG_MAX_BIND_PARAMETERS, f"one statement carried {max(counts)} parameters"


async def test_a_full_usage_buffer_is_written_in_statements_postgres_accepts(recording: _Recording) -> None:
    from felix.buffers import DEFAULT_MAX_PENDING
    from felix.usage import store as usage_store

    batch = [
        {"tenant_id": "acme", "id": f"u{i}", "ts": i, "manifest_id": "m", "model_id": "x"}
        for i in range(DEFAULT_MAX_PENDING)
    ]
    await usage_store._write_batch(SETTINGS, batch)
    counts = _bind_counts(recording.statements)
    assert sum(counts) >= DEFAULT_MAX_PENDING, "the batch was not written at all"
    assert max(counts) <= PG_MAX_BIND_PARAMETERS, f"one statement carried {max(counts)} parameters"
