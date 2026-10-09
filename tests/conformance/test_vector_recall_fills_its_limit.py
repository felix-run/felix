"""A vector channel served by the HNSW index returns as many rows as it asked for.

`memory_vectors` and `document_chunks` each carry one HNSW index across every tenant, and recall
filters to one tenant. When the planner serves that order from the index, the scan visits
`hnsw.ef_search` candidates (40 by default) and the tenant filter runs on those, so a tenant whose
rows are not among the nearest neighbours of the query got a short channel or an empty one
(`felix.db.vector`). Here every row of another tenant lies nearer the query than any of the
caller's, so without iterative scan the index hands back the other tenant's rows and nothing
survives the filter.

Postgres only: the twin scans every row. The connection's own options force the index plan --
the data alone would not, at this size -- and each test first runs the production statement with
iterative scan off and sees it come back short, so it cannot pass by scanning exactly. Needs
pgvector >= 0.8, which the CI image has.
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
QUERY = [1.0] + [0.0] * (DIM - 1)


def _vector(side: float, rng: random.Random) -> list[float]:
    v = [0.0] * DIM
    v[0], v[1] = 1.0, side
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
        return [QUERY for _ in texts]


def _on_the_index(settings: Any) -> Any:
    """The same database, with a connection that prefers the HNSW-ordered plan: no sequential
    scan, and no full sort, which the exact plan (the tenant's rows, then sorted) needs."""
    url = settings.database_url
    options = "options=-c%20enable_seqscan%3Doff%20-c%20enable_sort%3Doff"
    return settings.model_copy(update={"database_url": f"{url}{'&' if '?' in url else '?'}{options}"})


async def _execute(settings: Any, *statements: tuple[str, Any]) -> list[Any]:
    """Run ``statements`` in one transaction; the last one's rows."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(settings.database_url)
    try:
        async with engine.begin() as conn:
            result = None
            for sql, params in statements:
                result = await conn.execute(text(sql), params) if params else await conn.execute(text(sql))
            return list(result.all()) if result is not None and result.returns_rows else []
    finally:
        await engine.dispose()


async def _crowd(settings: Any, table: str) -> str:
    """Seed ``table`` with a caller's rows and a crowd of another tenant's nearer the query."""
    rng = random.Random(table)
    me, other = f"me{uuid.uuid4().hex[:8]}", f"other{uuid.uuid4().hex[:8]}"
    rows = [{"t": other, "id": f"o{i}", "v": _literal(_vector(0.0, rng))} for i in range(NEIGHBOURS)]
    rows += [{"t": me, "id": f"m{i}", "v": _literal(_vector(1.0, rng))} for i in range(OWN)]
    columns = "kind, created_at" if table == "memory_vectors" else "doc_id, created_at"
    values = "'fact', 1" if table == "memory_vectors" else "'d', 1"
    await _execute(
        settings,
        (
            f"INSERT INTO {table} (tenant_id, id, {columns}, embedding) "
            f"VALUES (:t, :id, {values}, CAST(:v AS vector))",
            rows,
        ),
    )
    await _execute(settings, (f"ANALYZE {table}", None))
    return me


async def _without_iterative_scan(settings: Any, sql: str, params: dict[str, Any]) -> int:
    rows = await _execute(
        _on_the_index(settings), ("SET LOCAL hnsw.iterative_scan = off", None), (sql, params)
    )
    return len(rows)


@pytest.mark.asyncio
async def test_memory_recall_vector_channel_is_full(store_settings: Any) -> None:
    from felix.memory.recall import _VECTOR_SQL, _channels_in_postgres

    me = await _crowd(store_settings, "memory_vectors")
    params = {"tenant": me, "manifest": "", "kinds": [], "lim": 16, "vec": _literal(QUERY)}
    assert await _without_iterative_scan(store_settings, _VECTOR_SQL, params) < 16, (
        "the statement fills its limit without iterative scan, so this test cannot fail"
    )

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
async def test_document_search_vector_channel_is_full(store_settings: Any) -> None:
    from felix.documents.store import _VECTOR_SQL, CHANNEL_DEPTH, _channels_in_postgres

    me = await _crowd(store_settings, "document_chunks")
    params = {"t": me, "v": _literal(QUERY), "n": CHANNEL_DEPTH}
    assert await _without_iterative_scan(store_settings, _VECTOR_SQL, params) < CHANNEL_DEPTH, (
        "the statement fills its limit without iterative scan, so this test cannot fail"
    )

    ranked, _ = await _channels_in_postgres(_on_the_index(store_settings), me, "?", QUERY)

    assert len(ranked.get("vector", [])) == CHANNEL_DEPTH, ranked


async def _memory_vector_channel(settings: Any, tenant: str) -> list[str]:
    from felix.memory.recall import _channels_in_postgres

    ranked = await _channels_in_postgres(
        settings, tenant, "?", manifest_id="", per_channel=16, kinds=None, embedder=_Axis()
    )
    return ranked.get("vector", [])


async def _document_vector_channel(settings: Any, tenant: str) -> list[str]:
    from felix.documents.store import _channels_in_postgres

    ranked, _ = await _channels_in_postgres(settings, tenant, "?", QUERY)
    return ranked.get("vector", [])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("table", "channel"),
    [("memory_vectors", _memory_vector_channel), ("document_chunks", _document_vector_channel)],
    ids=["memory", "documents"],
)
async def test_a_server_that_refuses_the_setting_still_searches(
    store_settings: Any, table: str, channel: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pgvector before 0.8 refuses the parameter. The refusal must cost the setting, not the
    query: unguarded, it aborts the transaction the vector channel then runs in."""
    from felix.db import vector

    # Not an unknown `hnsw.*` name: before pgvector loads in a session, Postgres takes any
    # dotted name as a placeholder and refuses nothing. This value is refused with the same
    # class of error (22023) an old pgvector answers.
    monkeypatch.setattr(vector, "_STATEMENT", "SET LOCAL statement_timeout = 'not a duration'")
    monkeypatch.setattr(vector, "_warned_unsupported", False)
    me = await _crowd(store_settings, table)

    found = await channel(store_settings, me)

    assert found, "the vector channel died with the setting"
    # The flag, not the log line: an earlier test's logging setup can keep `caplog` from seeing it.
    assert vector._warned_unsupported, "the stand-in was not refused as unsupported, so this proves nothing"


@pytest.mark.asyncio
async def test_a_failed_lexical_channel_leaves_the_vector_channel(
    store_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Document search ran both channels in one transaction, so a lexical failure aborted it and
    the vector channel failed on the aborted transaction."""
    from felix.documents import store

    monkeypatch.setattr(store, "_tsquery_or", lambda query: "a & & b")  # a tsquery syntax error
    me = await _crowd(store_settings, "document_chunks")

    ranked, _ = await store._channels_in_postgres(store_settings, me, "anything", QUERY)

    assert "lexical" not in ranked
    assert ranked.get("vector"), ranked


@pytest.mark.asyncio
async def test_only_a_refusal_is_blamed_on_the_pgvector_version(
    store_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An earlier statement's failure leaves the transaction aborted, and the setting then fails
    too -- for a reason that is not the extension's. Blaming the version there would send an
    operator to upgrade pgvector over a timeout, and spend the one warning a real old version gets."""
    from felix.db import vector
    from felix.db.session import get_session_factory
    from sqlalchemy import text

    monkeypatch.setattr(vector, "_warned_unsupported", False)
    async with get_session_factory(settings=store_settings)() as db:
        with pytest.raises(Exception):  # noqa: B017 - any error: the point is the aborted transaction
            await db.execute(text("SELECT 1/0"))
        await vector.enable_iterative_hnsw_scan(db)

    assert not vector._warned_unsupported
