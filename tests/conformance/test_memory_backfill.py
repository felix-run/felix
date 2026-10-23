"""The embedding backfill, run against every long-term-memory backend.

`memory.backfill.embed_missing` decides; the store reads (`missing_embeddings`) and writes
(`write_embeddings`). The store halves exist twice -- a dict walk and SQL over a pgvector column
the ORM cannot see -- so the job is run whole on both arms rather than with its store stubbed.

Every embedder here is registered through `register_embedder_backend` and chosen by
`FELIX_MEMORY_EMBEDDER`, the way a deployment chooses one, so `build_embedder` is on the path.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.memory import backfill
from felix.memory import store as memory_store

BACKENDS = ["memory", "postgres"]
parametrized = pytest.mark.parametrize("memory_settings", BACKENDS, indirect=True)

TENANT = "conformance"
OTHER_TENANT = "conformance-other"
MANIFEST = "m"
WIDTH = 768  # `vector(768)`, the column 0001_baseline builds
T0 = 1_800_000_000_000


class _Fake:
    """Deterministic vectors, every batch recorded. Sized to the real column by default."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        dim: int | None = WIDTH,
        makes: int = WIDTH,
        fail: bool = False,
        short: bool = False,
        model: str = "fake-768",
    ) -> None:
        self.enabled = enabled
        self.dim = dim
        self.model = model
        self.makes = makes
        self.fail = fail
        self.short = short
        self.calls: list[list[str]] = []

    async def embed(self, texts: Any) -> list[list[float]]:
        self.calls.append(list(texts))
        if self.fail:
            raise RuntimeError("embedding endpoint is down")
        out = [[1.0] + [0.0] * (self.makes - 1) for _ in texts]
        return out[:-1] if self.short else out


@pytest.fixture
def use(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Register `embedder` under a name of its own, on a copy of the registry."""
    from felix.memory import embedder as embedder_mod

    monkeypatch.setattr(embedder_mod, "_backends", dict(embedder_mod._backends))

    def _use(settings: Any, embedder: _Fake) -> Any:
        embedder_mod.register_embedder_backend("conformance-fake", lambda _s: embedder)
        return settings.model_copy(update={"memory_embedder": "conformance-fake"})

    return _use


@pytest.fixture
def column_width(memory_settings: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the twin the width Postgres reads from `memory_vector_config`.

    The twin has no column, so `configured_embedding_dim` answers ``None`` there and no width is
    ever refused -- which is the twin's behaviour, not a gap. These tests are about what the job
    does *when* a width is known, so on the twin the store is told the one Postgres has.
    """
    if str(memory_settings.database_url).startswith("memory://"):

        async def _width(_settings: Any) -> int:
            return WIDTH

        monkeypatch.setattr(memory_store, "configured_embedding_dim", _width)


async def _put(settings: Any, content: str, *, tenant: str = TENANT, **kw: Any) -> dict[str, Any]:
    return await memory_store.put_memory(settings, tenant, content=content, manifest_id=MANIFEST, **kw)


async def _row(settings: Any, mem_id: str, *, tenant: str = TENANT) -> dict[str, Any]:
    return (await memory_store.get_many(settings, tenant, [mem_id]))[mem_id]


@parametrized
async def test_backfill_embeds_only_vectorless_rows_in_every_tenant(memory_settings: Any, use: Any) -> None:
    emb = _Fake()
    settings = use(memory_settings, emb)
    bare = await _put(memory_settings, "The automobile is red.")
    theirs = await _put(memory_settings, "Their soup is cold.", tenant=OTHER_TENANT)
    done = await _put(
        memory_settings,
        "Already embedded.",
        embedding=[0.0, 1.0] + [0.0] * (WIDTH - 2),
        embedding_model="earlier",
    )

    assert await backfill.embed_missing(settings) == 2

    assert sorted(emb.calls[0]) == ["The automobile is red.", "Their soup is cold."], (
        "an embedded row was sent"
    )
    assert (await _row(memory_settings, bare["id"]))["embedding_dim"] == WIDTH
    assert (await _row(memory_settings, bare["id"]))["embedding_model"] == "fake-768"
    assert (await _row(memory_settings, theirs["id"], tenant=OTHER_TENANT))["embedding_dim"] == WIDTH
    assert (await _row(memory_settings, done["id"]))["embedding_model"] == "earlier"
    # Nothing left: the next tick asks the embedder for nothing.
    assert await backfill.embed_missing(settings) == 0
    assert len(emb.calls) == 1


@parametrized
async def test_backfill_is_bounded_newest_first_and_skips_inactive_rows(
    memory_settings: Any, use: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    emb = _Fake()
    settings = use(memory_settings, emb)
    for i in range(3):
        monkeypatch.setattr(memory_store, "now_ms", lambda i=i: T0 + i)
        await _put(memory_settings, f"Fact number {i}.")
    # Two tenants at one millisecond: only the tenant tiebreak decides which comes first, and
    # it must decide the same way on both arms (byte order, `COLLATE "C"`).
    monkeypatch.setattr(memory_store, "now_ms", lambda: T0 + 10)
    await _put(memory_settings, "Ours, same instant.")
    await _put(memory_settings, "Theirs, same instant.", tenant=OTHER_TENANT)
    gone = await _put(memory_settings, "Forgotten fact.")
    await memory_store.forget(memory_settings, TENANT, gone["id"])

    assert await backfill.embed_missing(settings, limit=3) == 3
    assert emb.calls == [["Theirs, same instant.", "Ours, same instant.", "Fact number 2."]]
    assert await backfill.embed_missing(settings, limit=10) == 2
    assert emb.calls[-1] == ["Fact number 1.", "Fact number 0."], "a forgotten row was embedded"


@parametrized
async def test_backfill_is_a_no_op_without_an_enabled_embedder(memory_settings: Any, use: Any) -> None:
    emb = _Fake(enabled=False)
    settings = use(memory_settings, emb)
    row = await _put(memory_settings, "No vector here.")

    assert await backfill.embed_missing(settings) == 0
    assert emb.calls == []
    assert (await _row(memory_settings, row["id"]))["embedding_dim"] is None


@parametrized
async def test_backfill_survives_an_embedder_that_raises(memory_settings: Any, use: Any) -> None:
    emb = _Fake(fail=True)
    settings = use(memory_settings, emb)
    row = await _put(memory_settings, "Still no vector.")

    assert await backfill.embed_missing(settings) == 0
    assert len(emb.calls) == 1
    assert (await _row(memory_settings, row["id"]))["embedding_dim"] is None


@parametrized
async def test_a_short_batch_writes_nothing(memory_settings: Any, use: Any) -> None:
    """Vectors are matched to rows by position, so one missing vector would misattribute every
    one after it. The batch is dropped whole."""
    emb = _Fake(short=True)
    settings = use(memory_settings, emb)
    a = await _put(memory_settings, "First fact.")
    b = await _put(memory_settings, "Second fact.")

    assert await backfill.embed_missing(settings) == 0
    assert len(emb.calls) == 1
    for row in (a, b):
        assert (await _row(memory_settings, row["id"]))["embedding_dim"] is None


@parametrized
async def test_a_declared_width_the_column_refuses_costs_no_call(
    memory_settings: Any, use: Any, column_width: None
) -> None:
    emb = _Fake(dim=4, makes=4, model="tiny")
    settings = use(memory_settings, emb)
    await _put(memory_settings, "Never sent.")

    assert await backfill.embed_missing(settings) == 0
    assert emb.calls == [], "an embedder the column will refuse was paid for"


@parametrized
async def test_an_undeclared_wrong_width_costs_one_batch_then_none(
    memory_settings: Any, use: Any, column_width: None
) -> None:
    """A declared `dim` is not a measured one (built-ins report the setting), so the first batch
    is what finds it out -- and the rows it was for stay vectorless, so without the memory each
    tick would pay for the same batch again."""
    emb = _Fake(dim=None, makes=4, model="tiny")
    settings = use(memory_settings, emb)
    row = await _put(memory_settings, "Sent once.")

    assert await backfill.embed_missing(settings) == 0
    assert await backfill.embed_missing(settings) == 0
    assert len(emb.calls) == 1
    assert (await _row(memory_settings, row["id"]))["embedding_dim"] is None


@parametrized
async def test_a_row_embedded_meanwhile_keeps_its_vector(memory_settings: Any) -> None:
    """The backfill reads, releases the connection, calls the embedder, then writes. A capture
    that embedded the same row in that window wrote the newer vector; the write fills gaps only."""
    row = await _put(memory_settings, "Embedded by capture in between.")
    batch = await memory_store.missing_embeddings(memory_settings, limit=10)
    assert [mem_id for _, mem_id, _ in batch] == [row["id"]]
    await _put(
        memory_settings,
        "Embedded by capture in between.",
        embedding=[0.0, 1.0] + [0.0] * (WIDTH - 2),
        embedding_model="capture",
    )

    written = await memory_store.write_embeddings(
        memory_settings, batch, [[1.0] + [0.0] * (WIDTH - 1)], model="backfill", expected_dim=None
    )

    assert written == 0
    assert (await _row(memory_settings, row["id"]))["embedding_model"] == "capture"


@parametrized
async def test_write_embeddings_refuses_a_vector_of_another_width(memory_settings: Any) -> None:
    row = await _put(memory_settings, "Wrong width offered.")
    batch = await memory_store.missing_embeddings(memory_settings, limit=10)

    written = await memory_store.write_embeddings(
        memory_settings, batch, [[1.0, 0.0, 0.0, 0.0]], model="tiny", expected_dim=WIDTH
    )

    assert written == 0
    assert (await _row(memory_settings, row["id"]))["embedding_dim"] is None
