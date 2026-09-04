"""The document corpus: chunking, hybrid retrieval, tenant isolation, and the routes.

Two things this file is built to catch, both learned from the fetch and search suites.

*A retrieval test that passes on an empty corpus proves nothing.* Several assertions here are
on counts and ordering, so every one of them first establishes that the corpus is non-empty —
and `conftest` clears it between tests, because a leaked corpus reads as a ranking bug.

*The vector channel is off by default.* `FELIX_MEMORY_EMBEDDER=none` means the default path is
lexical-only. Tests that mean to exercise fusion supply an embedder explicitly rather than
assuming one, and a test asserting "hybrid" that silently ran one channel would be the same
vacuous shape as an SSRF test refused by the scheme check.
"""

from __future__ import annotations

import pytest
from felix.config import Settings
from felix.documents import store as doc_store
from felix.documents.chunking import MIN_TAIL_CHARS, chunk_text
from felix.documents.store import (
    MAX_CHUNKS_PER_DOC,
    delete_document,
    document_id,
    list_documents,
    put_document,
    search_documents,
)

PROSE = (
    "Felix compiles a YAML manifest into a governed agent.\n\n"
    "The egress guard resolves a hostname once and dials the address it validated.\n\n"
    "Content screening quarantines untrusted tool output before the model reads it.\n\n"
    "Durable runs are fibers claimed under a lease and resumed by the scheduler.\n\n"
)


def _settings(**kw: object) -> Settings:
    base: dict[str, object] = {"database_url": "memory://ci", "object_store": "memory"}
    base.update(kw)
    return Settings(**base)  # type: ignore[arg-type]


class _StubEmbedder:
    """A deterministic embedder, so the vector channel is exercised rather than assumed.

    Bag-of-characters over a fixed alphabet: crude, but it makes similar text produce similar
    vectors, which is the only property the fusion path depends on. A `MagicMock` returning a
    constant would let the vector channel rank everything equally and still look wired.
    """

    enabled = True
    model = "stub"
    dim = 26

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            v = [0.0] * 26
            for ch in text.lower():
                if "a" <= ch <= "z":
                    v[ord(ch) - 97] += 1.0
            norm = sum(x * x for x in v) ** 0.5 or 1.0
            out.append([x / norm for x in v])
        return out


async def _ingest(settings: Settings, *, tenant: str = "t", **kw: object) -> tuple[str, int]:
    args: dict[str, object] = {
        "title": "Felix internals",
        "source": "https://docs.felix.run/internals",
        "text": PROSE,
        "max_chars": 80,
        "overlap_chars": 20,
    }
    args.update(kw)
    return await put_document(settings, tenant_id=tenant, **args)  # type: ignore[arg-type]


# --- chunking ---------------------------------------------------------------------


def test_short_text_is_one_chunk() -> None:
    (chunk,) = chunk_text("just a line")
    assert chunk.text == "just a line"
    assert (chunk.index, chunk.start) == (0, 0)


def test_empty_text_yields_nothing() -> None:
    """A document with no content should not occupy rows and rank for nothing."""
    assert chunk_text("") == []
    assert chunk_text("   \n\t  ") == []


def test_no_chunk_exceeds_the_budget() -> None:
    for budget in (45, 120, 400):
        chunks = chunk_text(PROSE * 5, max_chars=budget, overlap_chars=budget // 6)
        assert chunks, f"the fixture must split at max_chars={budget}"
        # The fold may exceed the budget by at most one runt, and the runt itself scales
        # with the budget — a flat allowance let `max_chars=45` return an 80-char chunk.
        allowance = budget + min(MIN_TAIL_CHARS, budget // 3)
        oversized = [len(c.text) for c in chunks if len(c.text) > allowance]
        assert not oversized, f"at max_chars={budget}, chunks over {allowance}: {oversized}"


def test_indices_are_contiguous_from_zero() -> None:
    chunks = chunk_text(PROSE * 3, max_chars=100, overlap_chars=10)
    assert [c.index for c in chunks] == list(range(len(chunks)))


def test_chunks_overlap_so_a_straddling_fact_stays_findable() -> None:
    """Without overlap a fact spanning a boundary is in neither chunk whole, so neither
    ranks for it."""
    chunks = chunk_text(PROSE * 3, max_chars=150, overlap_chars=40)
    assert len(chunks) > 2
    assert any(chunks[i].end > chunks[i + 1].start for i in range(len(chunks) - 1))


def test_a_boundary_is_preferred_over_a_mid_sentence_cut() -> None:
    text = "First sentence here. Second sentence here. Third sentence here. Fourth one here."
    chunks = chunk_text(text, max_chars=45, overlap_chars=0)
    assert len(chunks) > 1
    assert chunks[0].text.endswith("."), f"cut mid-sentence: {chunks[0].text!r}"


def test_text_with_no_boundaries_at_all_still_terminates() -> None:
    """A pathological document must not spin: the cursor advances even when the boundary
    search returns something degenerate."""
    chunks = chunk_text("x" * 1000, max_chars=100, overlap_chars=10)
    assert 5 < len(chunks) < 100
    assert "".join(c.text for c in chunks).count("x") >= 1000


def test_a_runt_tail_is_folded_into_its_predecessor() -> None:
    chunks = chunk_text("a" * 200 + " " + "b" * 5, max_chars=100, overlap_chars=0)
    assert all(len(c.text) >= MIN_TAIL_CHARS for c in chunks), [len(c.text) for c in chunks]


def test_a_zero_budget_is_refused_rather_than_looping() -> None:
    with pytest.raises(ValueError, match="max_chars"):
        chunk_text("anything", max_chars=0)


def test_an_overlap_at_or_above_the_window_cannot_stall_the_cursor() -> None:
    """`overlap >= max_chars` would never advance; it is clamped, not trusted."""
    chunks = chunk_text(PROSE * 2, max_chars=100, overlap_chars=100_000)
    assert chunks and len(chunks) < 200


# --- ingest and retrieval ---------------------------------------------------------


@pytest.mark.asyncio
async def test_a_document_is_ingested_and_retrievable() -> None:
    s = _settings()
    doc_id, chunks = await _ingest(s)
    assert chunks > 1, "the fixture must split, or retrieval is untested"

    hits = await search_documents(s, tenant_id="t", query="egress guard validated address", limit=3)
    assert hits, "nothing retrieved from a non-empty corpus"
    assert "validated" in hits[0].content
    assert hits[0].doc_id == doc_id
    assert hits[0].source == "https://docs.felix.run/internals"


@pytest.mark.asyncio
async def test_retrieval_reports_which_channel_surfaced_a_hit() -> None:
    """`channels` is the operator's answer to "why did it return this"."""
    s = _settings()
    await _ingest(s)
    (hit, *_) = await search_documents(s, tenant_id="t", query="content screening", limit=1)
    assert hit.channels == ("lexical",), "with no embedder only the lexical channel may run"


@pytest.mark.asyncio
async def test_the_vector_channel_runs_when_an_embedder_is_supplied() -> None:
    """The default is lexical-only, so a fusion test that does not pass an embedder is
    asserting nothing about fusion."""
    s = _settings()
    await _ingest(s, embedder=_StubEmbedder())
    hits = await search_documents(
        s, tenant_id="t", query="quarantines untrusted output", limit=5, embedder=_StubEmbedder()
    )
    assert hits
    assert any("vector" in h.channels for h in hits), [h.channels for h in hits]


@pytest.mark.asyncio
async def test_an_empty_query_returns_nothing_rather_than_everything() -> None:
    s = _settings()
    await _ingest(s)
    assert await search_documents(s, tenant_id="t", query="   ", limit=5) == []


@pytest.mark.asyncio
async def test_a_query_matching_nothing_returns_nothing() -> None:
    s = _settings()
    await _ingest(s)
    assert await search_documents(s, tenant_id="t", query="zzzqqqxxx", limit=5) == []


@pytest.mark.asyncio
async def test_the_limit_is_honoured() -> None:
    s = _settings()
    await _ingest(s, text=PROSE * 6)
    hits = await search_documents(s, tenant_id="t", query="felix agent manifest", limit=2)
    assert len(hits) == 2


# --- identity, replacement, isolation ---------------------------------------------


@pytest.mark.asyncio
async def test_reingesting_the_same_source_replaces_rather_than_duplicates() -> None:
    """Re-syncing a corpus must be idempotent, or every sync doubles it."""
    s = _settings()
    first_id, _ = await _ingest(s)
    second_id, _ = await _ingest(s, text=PROSE + "\n\nA newly added paragraph about leases.\n")

    assert first_id == second_id
    assert len(await list_documents(s, "t")) == 1
    assert await doc_store.count_documents(s, "t") == 1


@pytest.mark.asyncio
async def test_replacement_removes_chunks_that_no_longer_exist() -> None:
    """Delete-then-insert, not upsert: a shortened document must not keep its old tail."""
    s = _settings()
    await _ingest(s, text=PROSE * 4)
    before = len(await search_documents(s, tenant_id="t", query="fibers lease scheduler", limit=50))
    await _ingest(s, text="Only one short line about manifests now.")

    (summary,) = await list_documents(s, "t")
    assert summary.chunks == 1, f"stale chunks survived replacement: {summary.chunks}"
    assert before > 1
    assert await search_documents(s, tenant_id="t", query="fibers lease scheduler", limit=50) == []


@pytest.mark.asyncio
async def test_a_different_title_is_a_different_document() -> None:
    s = _settings()
    a, _ = await _ingest(s, title="One")
    b, _ = await _ingest(s, title="Two")
    assert a != b
    assert len(await list_documents(s, "t")) == 2


@pytest.mark.asyncio
async def test_one_tenant_cannot_retrieve_anothers_corpus() -> None:
    s = _settings()
    await _ingest(s, tenant="alice")
    assert await search_documents(s, tenant_id="bob", query="egress guard", limit=5) == []
    assert await list_documents(s, "bob") == []
    assert await search_documents(s, tenant_id="alice", query="egress guard", limit=5)


@pytest.mark.asyncio
async def test_deleting_removes_every_chunk() -> None:
    s = _settings()
    doc_id, chunks = await _ingest(s)
    assert await delete_document(s, "t", doc_id) == chunks
    assert await search_documents(s, tenant_id="t", query="egress guard", limit=5) == []
    assert await list_documents(s, "t") == []


@pytest.mark.asyncio
async def test_deleting_a_missing_document_reports_zero() -> None:
    assert await delete_document(_settings(), "t", "nope") == 0


@pytest.mark.asyncio
async def test_deleting_one_document_leaves_the_others() -> None:
    s = _settings()
    a, _ = await _ingest(s, title="One")
    await _ingest(s, title="Two")
    await delete_document(s, "t", a)
    assert [d.title for d in await list_documents(s, "t")] == ["Two"]


def test_document_identity_is_source_and_title() -> None:
    assert document_id("s", "t") == document_id("s", "t")
    assert document_id("s", "t") != document_id("s", "u")
    assert document_id("s", "t") != document_id("u", "t")


# --- degradation ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_failing_embedder_degrades_to_text_only_rather_than_losing_the_document() -> None:
    class _Broken:
        enabled = True

        async def embed(self, texts: list[str]) -> list[list[float]]:
            raise RuntimeError("model unavailable")

    s = _settings()
    _, chunks = await _ingest(s, embedder=_Broken())
    assert chunks > 0, "an embedder failure must not lose the text"
    assert await search_documents(s, tenant_id="t", query="egress guard", limit=3)


@pytest.mark.asyncio
async def test_a_short_embedder_batch_is_refused_rather_than_misaligned() -> None:
    """Vectors are matched to chunks by position, so a partial batch would attach one
    chunk's meaning to another's text."""

    class _Short:
        enabled = True

        async def embed(self, texts: list[str]) -> list[list[float]]:
            return [[1.0] * 26]  # one vector regardless of how many chunks

    s = _settings()
    await _ingest(s, embedder=_Short())
    (hit, *_) = await search_documents(
        s, tenant_id="t", query="egress guard", limit=1, embedder=_StubEmbedder()
    )
    assert "vector" not in hit.channels, "a misaligned batch was stored anyway"


@pytest.mark.asyncio
async def test_a_document_that_splits_past_the_ceiling_is_refused() -> None:
    s = _settings()
    with pytest.raises(ValueError, match=str(MAX_CHUNKS_PER_DOC)):
        await _ingest(s, text="word " * 400_000, max_chars=128, overlap_chars=0)


@pytest.mark.asyncio
async def test_an_empty_document_stores_no_chunks() -> None:
    s = _settings()
    _, chunks = await _ingest(s, text="   \n  ")
    assert chunks == 0
    assert await list_documents(s, "t") == []


# --- the management routes --------------------------------------------------------

KEYS = (
    '{"sk-read":{"tenant_id":"acme","sub":"ops","scopes":["documents:read"]},'
    '"sk-write":{"tenant_id":"acme","sub":"ops","scopes":["documents:write"]},'
    '"sk-none":{"tenant_id":"acme","sub":"ops","scopes":["chat:write"]},'
    '"sk-other":{"tenant_id":"other","sub":"ops","scopes":["documents:write"]}}'
)


def _route_settings() -> Settings:
    return Settings(
        allow_insecure=True,
        auth_mode="api_key",
        auth_api_keys=KEYS,
        environment="development",
        object_store="memory",
        database_url="memory://doc-routes",
    )


def _client():
    from felix_api.app import create_app
    from httpx import ASGITransport, AsyncClient

    app = create_app(settings=_route_settings(), plugins=[])
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _doc(**kw: object) -> dict[str, object]:
    body: dict[str, object] = {
        "title": "Felix internals",
        "source": "https://docs.felix.run/internals",
        "text": PROSE,
        "max_chars": 200,
    }
    body.update(kw)
    return body


@pytest.mark.asyncio
async def test_routes_gate_reads_and_writes_by_scope() -> None:
    async with _client() as client:
        denied = await client.get("/documents", headers=_auth("sk-none"))
        assert denied.status_code == 403
        assert "documents:read" in denied.json()["detail"]

        assert (await client.get("/documents", headers=_auth("sk-read"))).status_code == 200

        # A reader may not ingest; `documents:write` implies read, so a writer may do both.
        assert (await client.post("/documents", json=_doc(), headers=_auth("sk-read"))).status_code == 403
        assert (await client.post("/documents", json=_doc(), headers=_auth("sk-write"))).status_code == 200
        assert (await client.get("/documents", headers=_auth("sk-write"))).status_code == 200


@pytest.mark.asyncio
async def test_ingest_then_search_then_delete_over_the_api() -> None:
    async with _client() as client:
        created = await client.post("/documents", json=_doc(), headers=_auth("sk-write"))
        assert created.status_code == 200
        doc_id = created.json()["doc_id"]
        assert created.json()["chunks"] >= 1

        listed = await client.get("/documents", headers=_auth("sk-read"))
        assert [d["title"] for d in listed.json()["items"]] == ["Felix internals"]

        found = await client.get(
            "/documents/search", params={"q": "egress guard address"}, headers=_auth("sk-read")
        )
        assert found.status_code == 200
        items = found.json()["items"]
        assert items, "ingested a document and retrieved nothing"
        assert items[0]["channels"] == ["lexical"], "no embedder is configured in these settings"
        assert items[0]["doc_id"] == doc_id

        removed = await client.delete(f"/documents/{doc_id}", headers=_auth("sk-write"))
        assert removed.status_code == 200
        assert removed.json()["removed_chunks"] >= 1
        assert (await client.get("/documents", headers=_auth("sk-read"))).json()["items"] == []


@pytest.mark.asyncio
async def test_the_tenant_comes_from_the_principal_not_the_request() -> None:
    """One tenant ingesting must not be visible to another, however the request is shaped."""
    async with _client() as client:
        await client.post("/documents", json=_doc(), headers=_auth("sk-write"))

        other = await client.get("/documents", headers=_auth("sk-other"))
        assert other.json()["items"] == []
        other_search = await client.get(
            "/documents/search", params={"q": "egress guard"}, headers=_auth("sk-other")
        )
        assert other_search.json()["items"] == []


@pytest.mark.asyncio
async def test_deleting_a_missing_document_is_a_404() -> None:
    async with _client() as client:
        gone = await client.delete("/documents/nope", headers=_auth("sk-write"))
        assert gone.status_code == 404


@pytest.mark.asyncio
async def test_a_document_past_the_chunk_ceiling_is_a_400_not_a_500() -> None:
    """The caller sent something the corpus will not hold; the message names the limit.

    The text is sized to clear the chunk ceiling while staying under the 1 MiB body limit,
    because the middleware answers 413 first and that is a different conversation.
    """
    async with _client() as client:
        huge = await client.post(
            "/documents",
            json=_doc(text="word " * 100_000, max_chars=128, overlap_chars=0),
            headers=_auth("sk-write"),
        )
        assert huge.status_code == 400
        assert str(MAX_CHUNKS_PER_DOC) in huge.json()["detail"]


@pytest.mark.asyncio
async def test_the_declared_text_ceiling_is_reachable_through_the_body_limit() -> None:
    """`MAX_DOCUMENT_CHARS` above the body limit would advertise a size no request can carry:
    the caller gets 413 and never learns the real document ceiling."""
    from felix_api.app import CORE_BODY_LIMIT_BYTES
    from felix_api.routes.documents import MAX_DOCUMENT_CHARS

    assert MAX_DOCUMENT_CHARS < CORE_BODY_LIMIT_BYTES


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"title": "", "text": "x"},
        {"title": "t"},
        {"title": "t", "text": "x", "max_chars": 1},
        {"title": "t", "text": "x", "overlap_chars": -1},
        {"title": "t", "text": "x", "unexpected": 1},
    ],
    ids=["blank-title", "no-text", "tiny-budget", "negative-overlap", "extra-field"],
)
async def test_malformed_ingest_bodies_are_refused(body: dict[str, object]) -> None:
    async with _client() as client:
        assert (await client.post("/documents", json=body, headers=_auth("sk-write"))).status_code == 422


@pytest.mark.asyncio
async def test_search_requires_a_query() -> None:
    async with _client() as client:
        assert (await client.get("/documents/search", headers=_auth("sk-read"))).status_code == 422


def test_an_overlapped_chunk_starts_at_a_word_boundary() -> None:
    """A literal `end - overlap` cut mid-word — a chunk beginning "ss guard resolves".

    That costs twice: the lexical channel tokenises "ss" as a term matching nothing, and a
    model handed the chunk reads a truncated first word as though it were the text.
    """
    chunks = chunk_text(PROSE * 3, max_chars=90, overlap_chars=25)
    assert len(chunks) > 3
    starts = [c.text.split()[0] for c in chunks[1:] if c.text.split()]
    words = set(PROSE.replace("\n", " ").split())
    broken = [w for w in starts if w not in words]
    assert not broken, f"chunks starting mid-word: {broken}"
