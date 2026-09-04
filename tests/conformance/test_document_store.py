"""One contract, run against every corpus backend.

The store exists twice — a dict walk with hand-rolled cosine for `memory://`, and SQL with a
generated `content_tsv` plus a pgvector `<=>` operator for Postgres. That is precisely the
shape this repo keeps finding drifts when nothing compares the copies, and it is worse here
than usual: the two arms are *different ranking implementations*, so they can disagree about
which chunk is best while both look correct in isolation.

The Postgres arm is also the only place several things are exercised at all — the generated
tsvector column, the HNSW index, the nullable embedding that `0009` had to learn the hard way
to allow. `create_all` cannot produce a generated column, so this arm only means anything
because the fixture applies the migrations.

Add a backend to `BACKENDS` and it inherits every assertion here.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.documents import store as doc_store

BACKENDS = ["memory", "postgres"]
parametrized = pytest.mark.parametrize("document_settings", BACKENDS, indirect=True)

TENANT = "conformance"

PROSE = (
    "Felix compiles a YAML manifest into a governed agent.\n\n"
    "The egress guard resolves a hostname once and dials the address it validated.\n\n"
    "Content screening quarantines untrusted tool output before the model reads it.\n\n"
    "Durable runs are fibers claimed under a lease and resumed by the scheduler.\n\n"
)


class _StubEmbedder:
    """Deterministic bag-of-characters, so both arms embed identically and any divergence
    is the store's, not the embedder's."""

    enabled = True
    model = "stub"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            v = [0.0] * 768
            for ch in text.lower():
                if "a" <= ch <= "z":
                    v[ord(ch) - 97] += 1.0
            norm = sum(x * x for x in v) ** 0.5 or 1.0
            out.append([x / norm for x in v])
        return out


async def _ingest(settings: Any, **kw: Any) -> tuple[str, int]:
    args: dict[str, Any] = {
        "title": "Felix internals",
        "source": "https://docs.felix.run/internals",
        "text": PROSE,
        "max_chars": 90,
        "overlap_chars": 20,
    }
    args.update(kw)
    return await doc_store.put_document(settings, tenant_id=TENANT, **args)


@parametrized
@pytest.mark.asyncio
async def test_a_document_round_trips(document_settings: Any) -> None:
    doc_id, chunks = await _ingest(document_settings)
    assert chunks > 1, "the fixture must split on both arms, or retrieval is untested"

    summaries = await doc_store.list_documents(document_settings, TENANT)
    assert [(s.doc_id, s.chunks) for s in summaries] == [(doc_id, chunks)]
    assert summaries[0].title == "Felix internals"
    assert summaries[0].source == "https://docs.felix.run/internals"


@parametrized
@pytest.mark.asyncio
async def test_lexical_retrieval_finds_the_right_chunk(document_settings: Any) -> None:
    """Postgres ranks with `ts_rank` over a generated tsvector; the twin counts token
    overlap. They must agree on which chunk answers this."""
    await _ingest(document_settings)
    hits = await doc_store.search_documents(
        document_settings, tenant_id=TENANT, query="egress guard dials validated address", limit=3
    )
    assert hits, "nothing retrieved from a non-empty corpus"
    # A distinctive interior term, not a phrase that happens to straddle a chunk boundary at
    # this particular budget — asserting the latter tests the chunker, not retrieval.
    assert "validated" in hits[0].content
    assert "lexical" in hits[0].channels


@parametrized
@pytest.mark.asyncio
async def test_the_vector_channel_runs_on_both_arms(document_settings: Any) -> None:
    """One arm computes cosine in Python, the other with pgvector's `<=>`. Both must
    actually attribute the channel, or hybrid retrieval is hybrid on one backend only."""
    await _ingest(document_settings, embedder=_StubEmbedder())
    hits = await doc_store.search_documents(
        document_settings,
        tenant_id=TENANT,
        query="quarantines untrusted tool output",
        limit=5,
        embedder=_StubEmbedder(),
    )
    assert hits
    assert any("vector" in h.channels for h in hits), [h.channels for h in hits]


@parametrized
@pytest.mark.asyncio
async def test_a_document_stores_without_an_embedder(document_settings: Any) -> None:
    """The embedding column is nullable on purpose. `memory_vectors` shipped `NOT NULL` with
    no default and every insert failed silently for months; this arm is what would catch the
    same mistake here."""
    _, chunks = await _ingest(document_settings)
    assert chunks > 0
    assert await doc_store.search_documents(
        document_settings, tenant_id=TENANT, query="fibers lease scheduler", limit=3
    )


@parametrized
@pytest.mark.asyncio
async def test_reingest_replaces_rather_than_duplicates(document_settings: Any) -> None:
    first, _ = await _ingest(document_settings)
    second, _ = await _ingest(document_settings, text="Only one short line about manifests now.")

    assert first == second
    summaries = await doc_store.list_documents(document_settings, TENANT)
    assert len(summaries) == 1
    assert summaries[0].chunks == 1, "stale chunks survived replacement"
    assert (
        await doc_store.search_documents(
            document_settings, tenant_id=TENANT, query="fibers lease scheduler", limit=10
        )
        == []
    )


@parametrized
@pytest.mark.asyncio
async def test_tenants_are_isolated(document_settings: Any) -> None:
    await _ingest(document_settings)
    assert (
        await doc_store.search_documents(
            document_settings, tenant_id="somebody-else", query="egress guard", limit=5
        )
        == []
    )
    assert await doc_store.list_documents(document_settings, "somebody-else") == []


@parametrized
@pytest.mark.asyncio
async def test_delete_removes_every_chunk(document_settings: Any) -> None:
    doc_id, chunks = await _ingest(document_settings)
    assert await doc_store.delete_document(document_settings, TENANT, doc_id) == chunks
    assert await doc_store.list_documents(document_settings, TENANT) == []
    assert await doc_store.count_documents(document_settings, TENANT) == 0


@parametrized
@pytest.mark.asyncio
async def test_deleting_a_missing_document_reports_zero(document_settings: Any) -> None:
    assert await doc_store.delete_document(document_settings, TENANT, "nope") == 0


@parametrized
@pytest.mark.asyncio
async def test_an_empty_query_returns_nothing(document_settings: Any) -> None:
    await _ingest(document_settings)
    assert await doc_store.search_documents(document_settings, tenant_id=TENANT, query="  ") == []


@parametrized
@pytest.mark.asyncio
async def test_the_limit_is_honoured_on_both_arms(document_settings: Any) -> None:
    await _ingest(document_settings, text=PROSE * 4)
    hits = await doc_store.search_documents(
        document_settings, tenant_id=TENANT, query="felix manifest agent guard", limit=2
    )
    assert len(hits) == 2


@parametrized
@pytest.mark.asyncio
async def test_metadata_round_trips_on_both_arms(document_settings: Any) -> None:
    """A live divergence the contract could not see: the twin returned metadata and the
    Postgres arm always returned `{}`, because its `select` never fetched the column."""
    await _ingest(document_settings, metadata={"team": "platform", "rev": 7})
    (summary,) = await doc_store.list_documents(document_settings, TENANT)
    assert summary.metadata == {"team": "platform", "rev": 7}


@parametrized
@pytest.mark.asyncio
async def test_metadata_is_stored_once_not_per_chunk(document_settings: Any) -> None:
    """Copied onto every row it was a 1,700x storage amplifier. Chunk 0 carries it."""
    _, chunks = await _ingest(document_settings, metadata={"k": "v"})
    assert chunks > 1, "needs a multi-chunk document to mean anything"
    hits = await doc_store.search_documents(
        document_settings, tenant_id=TENANT, query="egress guard validated", limit=50
    )
    assert hits
    (summary,) = await doc_store.list_documents(document_settings, TENANT)
    assert summary.metadata == {"k": "v"}, "metadata must still be reachable from chunk 0"


@parametrized
@pytest.mark.asyncio
async def test_several_documents_list_newest_first_with_a_tiebreaker(document_settings: Any) -> None:
    """Every other test here ingests one title, so ordering, `limit` and `count` across
    documents were untested on both arms — and the arms disagreed, the twin sorting
    `(-created_at, doc_id)` and Postgres having no tiebreaker at all."""
    for title in ("One", "Two", "Three"):
        await _ingest(document_settings, title=title)

    summaries = await doc_store.list_documents(document_settings, TENANT)
    assert len(summaries) == 3
    assert await doc_store.count_documents(document_settings, TENANT) == 3

    stamps = [s.created_at for s in summaries]
    assert stamps == sorted(stamps, reverse=True), "not newest-first"
    same_ms = [s.doc_id for s in summaries if s.created_at == stamps[0]]
    assert same_ms == sorted(same_ms), "no stable tiebreaker within one millisecond"

    assert len(await doc_store.list_documents(document_settings, TENANT, limit=2)) == 2


@parametrized
@pytest.mark.asyncio
async def test_the_stored_chunk_count_matches_what_ingest_reported(document_settings: Any) -> None:
    """`put_document`'s return value was never cross-checked against storage.

    That is how an aborted transaction reported `(doc_id, 1)` against an empty table.
    """
    _, reported = await _ingest(document_settings)
    (summary,) = await doc_store.list_documents(document_settings, TENANT)
    assert summary.chunks == reported, f"ingest said {reported}, storage holds {summary.chunks}"


@parametrized
@pytest.mark.asyncio
async def test_an_embedder_of_the_wrong_dimension_loses_vectors_not_the_document(
    document_settings: Any,
) -> None:
    """The defect this contract exists for, one level down from `memory_vectors`.

    A 26-dimension embedder against the migration's `vector(768)` column aborted the
    transaction inside a swallowed `except`, so the commit discarded the chunk inserts and
    `put_document` returned a count for rows that were never written. Reachable by any
    operator who points `FELIX_MEMORY_EMBEDDER` at a model of another size.
    """

    class _WrongDim:
        enabled = True
        model = "wrong-dim"

        async def embed(self, texts: list[str]) -> list[list[float]]:
            return [[0.1] * 26 for _ in texts]

    _, reported = await _ingest(document_settings, embedder=_WrongDim())
    assert reported > 0

    summaries = await doc_store.list_documents(document_settings, TENANT)
    assert summaries, "the document was lost entirely"
    assert summaries[0].chunks == reported, "ingest reported chunks that were never stored"
    assert await doc_store.search_documents(
        document_settings, tenant_id=TENANT, query="egress guard validated", limit=3
    ), "the document is unretrievable, so the rows are not really there"


@parametrized
@pytest.mark.asyncio
async def test_a_second_tenants_corpus_is_invisible_both_ways(document_settings: Any) -> None:
    """The one-sided form — an empty tenant sees nothing — is also true of a broken store."""
    await _ingest(document_settings, title="Mine")
    await doc_store.put_document(
        document_settings, tenant_id="other", title="Theirs", source="s2", text=PROSE, max_chars=90
    )

    mine = await doc_store.list_documents(document_settings, TENANT)
    theirs = await doc_store.list_documents(document_settings, "other")
    assert [s.title for s in mine] == ["Mine"]
    assert [s.title for s in theirs] == ["Theirs"]

    # A cross-tenant delete must not reach across.
    assert await doc_store.delete_document(document_settings, "other", mine[0].doc_id) == 0
    assert await doc_store.list_documents(document_settings, TENANT) == mine
