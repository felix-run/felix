"""A vector channel served by the HNSW index returns as many rows as it asked for.

`memory_vectors` and `document_chunks` each carry one HNSW index across every tenant, and recall
filters to one tenant. When the planner serves that order from the index, the scan visits
`hnsw.ef_search` candidates (40 by default) and the tenant filter runs on those, so a tenant whose
rows are not among the nearest neighbours of the query got a short channel or an empty one
(`felix.db.vector`). Here every row of another tenant lies nearer the query than any of the
caller's, so without iterative scan the index hands back the other tenant's rows and nothing
survives the filter.

Postgres only: the twin scans every row. The planner is steered onto the index the way it chooses
it for a tenant holding most of a table, by the connection's own options, and each test asserts
the plan it ran under so it cannot pass by scanning exactly.
"""

from __future__ import annotations

import random
import uuid
from typing import Any

import pytest

pytestmark = pytest.mark.parametrize("store_settings", ["postgres"], indirect=True)

DIM = 768
NEIGHBOURS = 400  # the other tenant's rows, all nearer the query than any of the caller's
OWN = 60


def _vector(lead: float, side: float, rng: random.Random) -> list[float]:
    v = [0.0] * DIM
    v[0], v[1] = lead, side
    for i in range(2, 12):
        v[i] = rng.uniform(-0.01, 0.01)
    return v


def _literal(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


class _Axis:
    """Embeds every query onto the axis the other tenant's rows crowd."""

    enabled = True
    model = "axis"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0] + [0.0] * (DIM - 1) for _ in texts]


def _on_the_index(settings: Any) -> Any:
    """The same database, with a connection that prefers the HNSW-ordered plan: no sequential
    scan, and no full sort, which the exact plan (the tenant's rows, then sorted) needs."""
    url = settings.database_url
    options = "options=-c%20enable_seqscan%3Doff%20-c%20enable_sort%3Doff"
    return settings.model_copy(update={"database_url": f"{url}{'&' if '?' in url else '?'}{options}"})


async def _execute(settings: Any, sql: str, rows: list[dict[str, Any]] | None = None) -> list[Any]:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(settings.database_url)
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text(sql), rows) if rows else await conn.execute(text(sql))
            return list(result.all()) if result.returns_rows else []
    finally:
        await engine.dispose()


async def _plan(settings: Any, sql: str) -> str:
    return " ".join(str(r[0]) for r in await _execute(_on_the_index(settings), f"EXPLAIN {sql}"))


@pytest.mark.asyncio
async def test_memory_recalls_vector_channel_is_full(store_settings: Any) -> None:
    from felix.memory.recall import _channels_in_postgres

    rng = random.Random(1)
    me, other = f"me{uuid.uuid4().hex[:8]}", f"other{uuid.uuid4().hex[:8]}"
    rows = [{"t": other, "id": f"o{i}", "v": _literal(_vector(1.0, 0.0, rng))} for i in range(NEIGHBOURS)] + [
        {"t": me, "id": f"m{i}", "v": _literal(_vector(1.0, 1.0, rng))} for i in range(OWN)
    ]
    await _execute(
        store_settings,
        "INSERT INTO memory_vectors (tenant_id, id, kind, created_at, embedding) "
        "VALUES (:t, :id, 'fact', 1, CAST(:v AS vector))",
        rows,
    )
    await _execute(store_settings, "ANALYZE memory_vectors")
    plan = await _plan(
        store_settings,
        f"SELECT id FROM memory_vectors WHERE tenant_id = '{me}' AND status = 'active' "
        f"ORDER BY embedding <=> '{_literal(_vector(1.0, 0.0, rng))}', importance DESC, "
        'created_at DESC, id COLLATE "C" DESC LIMIT 16',
    )
    assert "idx_memvec_hnsw" in plan, f"the test did not reach the index, so it cannot fail: {plan}"

    ranked = await _channels_in_postgres(
        _on_the_index(store_settings),
        me,
        "?",  # no lexical tokens: the vector channel alone
        manifest_id="",
        per_channel=16,
        kinds=None,
        embedder=_Axis(),
    )

    assert len(ranked.get("vector", [])) == 16, ranked


@pytest.mark.asyncio
async def test_document_searchs_vector_channel_is_full(store_settings: Any) -> None:
    from felix.documents.store import CHANNEL_DEPTH, _channels_in_postgres

    rng = random.Random(2)
    me, other = f"me{uuid.uuid4().hex[:8]}", f"other{uuid.uuid4().hex[:8]}"
    rows = [{"t": other, "id": f"o{i}", "v": _literal(_vector(1.0, 0.0, rng))} for i in range(NEIGHBOURS)] + [
        {"t": me, "id": f"m{i}", "v": _literal(_vector(1.0, 1.0, rng))} for i in range(OWN)
    ]
    await _execute(
        store_settings,
        "INSERT INTO document_chunks (tenant_id, id, doc_id, created_at, embedding) "
        "VALUES (:t, :id, 'd', 1, CAST(:v AS vector))",
        rows,
    )
    await _execute(store_settings, "ANALYZE document_chunks")
    query = _vector(1.0, 0.0, rng)
    plan = await _plan(
        store_settings,
        f"SELECT id FROM document_chunks WHERE tenant_id = '{me}' AND embedding IS NOT NULL "
        f"ORDER BY embedding <=> '{_literal(query)}', id LIMIT {CHANNEL_DEPTH}",
    )
    assert "idx_doc_chunks_embedding" in plan, f"the test did not reach the index, so it cannot fail: {plan}"

    ranked, _ = await _channels_in_postgres(_on_the_index(store_settings), me, "?", query)

    assert len(ranked.get("vector", [])) == CHANNEL_DEPTH, ranked


@pytest.mark.asyncio
async def test_a_server_without_iterative_scan_still_searches(
    store_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pgvector before 0.8 refuses the parameter. The refusal must cost the setting, not the
    query: unguarded, it aborts the transaction the vector channel then runs in."""
    from felix.db import vector
    from felix.documents.store import _channels_in_postgres

    # Not an unknown `hnsw.*` name: before pgvector loads in a session, Postgres takes any
    # dotted name as a placeholder and refuses nothing.
    monkeypatch.setattr(vector, "ITERATIVE_SCAN", "SET LOCAL statement_timeout = 'not a duration'")
    rng = random.Random(3)
    me = f"me{uuid.uuid4().hex[:8]}"
    await _execute(
        store_settings,
        "INSERT INTO document_chunks (tenant_id, id, doc_id, created_at, embedding) "
        "VALUES (:t, :id, 'd', 1, CAST(:v AS vector))",
        [{"t": me, "id": f"m{i}", "v": _literal(_vector(1.0, 1.0, rng))} for i in range(5)],
    )

    ranked, _ = await _channels_in_postgres(store_settings, me, "?", _vector(1.0, 0.0, rng))

    assert len(ranked.get("vector", [])) == 5, ranked
