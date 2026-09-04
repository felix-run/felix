"""The document corpus: ingest, list, search, delete — Postgres and an in-memory twin.

Search is hybrid, fusing a lexical channel and a vector one with the same Reciprocal Rank
Fusion `memory/recall.py` uses, and reusing its `rrf_fuse` rather than growing a second
implementation. A channel that cannot run is *skipped, not faked*: with no embedder
configured — the default — retrieval is full-text only and says so, because a corpus that
silently returns nothing is indistinguishable from one that was never ingested.

Every hit carries the channels that surfaced it, for the same reason memory's does: when a
result is wrong, the first question is which retriever produced it.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import delete, distinct, func, select

from felix.config import Settings
from felix.db.models import DocumentChunk
from felix.db.session import _use_memory, get_session_factory
from felix.documents.chunking import DEFAULT_MAX_CHARS, DEFAULT_OVERLAP_CHARS, Chunk, chunk_text
from felix.memory.recall import rrf_fuse

logger = logging.getLogger("felix.documents.store")

# How many candidates each channel contributes before fusion. Wider than the caller's limit
# on purpose: a channel that returns exactly `limit` gives RRF nothing to reorder.
CHANNEL_DEPTH = 40

# Named rather than `SELECT *`: the star pulled `content_tsv` and a `vector(768)` for every
# candidate on both channels and then discarded both — megabytes of wire traffic per search,
# for columns no code path reads.
_HIT_COLUMNS = "id, doc_id, chunk_index, title, source, content, created_at"

# A corpus is operator-owned, but "operator-owned" is not "unbounded" — a runaway ingest
# should fail loudly rather than fill a disk.
MAX_CHUNKS_PER_DOC = 2_000

_memory_rows: dict[tuple[str, str], dict[str, Any]] = {}


def reset_documents_for_tests() -> None:
    """Clear the in-memory corpus between tests.

    Module-level state outlives a test, and `tests/unit/test_invariants.py` requires every
    in-memory store to expose a reset — a corpus leaking across tests is how one test's
    ingest becomes another's mysterious extra hit.
    """
    _memory_rows.clear()


def now_ms() -> int:
    return int(time.time() * 1000)


def document_id(source: str, title: str) -> str:
    """Stable id for a document, so re-ingesting the same source replaces rather than duplicates."""
    return hashlib.sha256(f"{source}\x00{title}".encode()).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class DocumentHit:
    doc_id: str
    chunk_id: str
    chunk_index: int
    title: str
    source: str
    content: str
    score: float
    channels: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DocumentSummary:
    doc_id: str
    title: str
    source: str
    chunks: int
    created_at: int
    metadata: dict[str, Any] = field(default_factory=dict)


def _first_chunk(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The `chunk_index == 0` row, which is the one carrying the document's metadata."""
    return min(rows, key=lambda r: int(r.get("chunk_index") or 0))


def _tokens(text: str) -> set[str]:
    return {w for w in "".join(c if c.isalnum() else " " for c in text.lower()).split() if len(w) > 2}


# Chunks per embedding request. A 2,000-chunk document in one call is a ~750k-token request
# that every hosted provider refuses, and the refusal was swallowed into a WARNING while
# ingest returned 200 — the whole document silently text-only. Batching turns a provider's
# batch ceiling into a non-event.
EMBED_BATCH = 64


async def _embed_chunks(
    chunks: list[Chunk], embedder: Any | None, *, source: str
) -> tuple[list[list[float] | None], str]:
    """Vectors positionally aligned with `chunks`, and the model that produced them.

    A batch that comes back the wrong length is discarded rather than zipped: vectors are
    matched to chunks by position, so a short batch would attach one chunk's meaning to
    another's text. Failure of any batch costs that batch's vectors, not the ingest.
    """
    vectors: list[list[float] | None] = [None] * len(chunks)
    if not chunks or embedder is None or not getattr(embedder, "enabled", False):
        return vectors, ""

    model = str(getattr(embedder, "model", "") or type(embedder).__name__)
    for start in range(0, len(chunks), EMBED_BATCH):
        window = chunks[start : start + EMBED_BATCH]
        try:
            produced = await embedder.embed([c.text for c in window])
        except Exception:
            logger.warning(
                "embedding failed for %r chunks %d-%d; those stay text-only",
                source,
                start,
                start + len(window) - 1,
                exc_info=True,
            )
            continue
        if len(produced) != len(window):
            logger.warning(
                "embedder returned %d vectors for %d chunks; that batch stays text-only",
                len(produced),
                len(window),
            )
            continue
        for offset, vec in enumerate(produced):
            vectors[start + offset] = list(vec)
    return vectors, model


async def put_document(
    settings: Settings,
    *,
    tenant_id: str,
    title: str,
    source: str,
    text: str,
    metadata: dict[str, Any] | None = None,
    embedder: Any | None = None,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
) -> tuple[str, int]:
    """Ingest (or re-ingest) one document. Returns its id and the chunk count.

    Replacement is delete-then-insert under one transaction on the Postgres arm: a document
    re-ingested after an edit must not leave the chunks that no longer exist, and a partial
    replacement is worse than either version alone.
    """
    doc_id = document_id(source, title)
    chunks = chunk_text(text, max_chars=max_chars, overlap_chars=overlap_chars)
    if len(chunks) > MAX_CHUNKS_PER_DOC:
        raise ValueError(f"document splits into {len(chunks)} chunks; the ceiling is {MAX_CHUNKS_PER_DOC}")

    vectors, model = await _embed_chunks(chunks, embedder, source=source)

    rows = _build_rows(
        chunks,
        vectors,
        tenant_id=tenant_id,
        doc_id=doc_id,
        title=title,
        source=source,
        metadata=metadata,
        model=model,
    )

    if _use_memory(settings):
        stale = [k for k, r in _memory_rows.items() if k[0] == tenant_id and r["doc_id"] == doc_id]
        for key in stale:
            _memory_rows.pop(key, None)
        for row, vec in rows:
            _memory_rows[(tenant_id, str(row["id"]))] = {**row, "_vector": vec}
        return doc_id, len(rows)

    await _put_in_postgres(settings, tenant_id, doc_id, rows)
    return doc_id, len(rows)


def _build_rows(
    chunks: list[Chunk],
    vectors: list[list[float] | None],
    *,
    tenant_id: str,
    doc_id: str,
    title: str,
    source: str,
    metadata: dict[str, Any] | None,
    model: str,
) -> list[tuple[dict[str, Any], list[float] | None]]:
    """Chunk rows paired with their vectors.

    Paired rather than two parallel lists indexed together: `vectors[i]` beside `rows[i]` is
    the shape that lets a refactor silently misalign one chunk's meaning with another's text,
    and it also defeats narrowing — `list[float] | None` cannot be refined inside a
    comprehension, so every use needed a guard the type checker could not see.
    """
    ts = now_ms()
    out: list[tuple[dict[str, Any], list[float] | None]] = []
    for chunk, vec in zip(chunks, vectors, strict=True):
        out.append(
            (
                {
                    "tenant_id": tenant_id,
                    "id": f"{doc_id}:{chunk.index}",
                    "doc_id": doc_id,
                    "chunk_index": chunk.index,
                    "title": title,
                    "source": source,
                    "content": chunk.text,
                    # First chunk only. Copied onto every row this was a storage amplifier,
                    # not a convenience: one request inside the 1 MiB body limit can carry
                    # ~880 KB of metadata and 1,969 chunks, each getting its own copy — 1.7 GB
                    # written, measured. Both documented ceilings held; their *product* was
                    # what nothing bounded.
                    "metadata": dict(metadata or {}) if chunk.index == 0 else {},
                    "created_at": ts,
                    "embedding_dim": len(vec) if vec else None,
                    "embedding_model": model if vec else "",
                },
                vec,
            )
        )
    return out


async def _put_in_postgres(
    settings: Settings,
    tenant_id: str,
    doc_id: str,
    rows: list[tuple[dict[str, Any], list[float] | None]],
) -> None:
    factory = get_session_factory(settings=settings)
    async with factory() as db:
        await db.execute(
            delete(DocumentChunk).where(DocumentChunk.tenant_id == tenant_id, DocumentChunk.doc_id == doc_id)
        )
        for row, _vec in rows:
            db.add(
                DocumentChunk(
                    tenant_id=row["tenant_id"],
                    id=row["id"],
                    doc_id=row["doc_id"],
                    chunk_index=row["chunk_index"],
                    title=row["title"],
                    source=row["source"],
                    content=row["content"],
                    metadata_json=row["metadata"],
                    created_at=row["created_at"],
                    embedding_dim=row["embedding_dim"],
                    embedding_model=row["embedding_model"],
                )
            )
        await db.flush()
        for row, vec in rows:
            if vec:
                await _write_embedding(db, tenant_id, str(row["id"]), vec)
        await db.commit()


async def _write_embedding(db: Any, tenant_id: str, chunk_id: str, vector: list[float]) -> None:
    """Attach one chunk's vector, inside a savepoint.

    The savepoint is the point. Catching the exception without one left the *transaction*
    aborted, so the caller's `commit()` discarded the chunk inserts too — `put_document`
    returned `(doc_id, 1)` and the table held zero rows. Reproduced against real Postgres with
    a 26-dimension embedder against the migration's `vector(768)` column, which any operator
    reaches by pointing `FELIX_MEMORY_EMBEDDER` at a model of another size.

    That is the `memory_vectors` failure one level down: a write reporting success and storing
    nothing, with the detail in a log nobody reads. Losing a document's vector channel is an
    acceptable degradation; losing the document is not.
    """
    from sqlalchemy import text as sql_text

    literal = "[" + ",".join(f"{float(x):.7g}" for x in vector) + "]"
    try:
        async with db.begin_nested():
            await db.execute(
                sql_text(
                    "UPDATE document_chunks SET embedding = CAST(:v AS vector) "
                    "WHERE tenant_id = :t AND id = :i"
                ),
                {"v": literal, "t": tenant_id, "i": chunk_id},
            )
    except Exception:
        logger.warning(
            "failed to write embedding for chunk %s; the chunk is stored, text-only",
            chunk_id,
            exc_info=True,
        )


async def list_documents(settings: Settings, tenant_id: str, *, limit: int = 100) -> list[DocumentSummary]:
    if _use_memory(settings):
        by_doc: dict[str, list[dict[str, Any]]] = {}
        for (t, _), row in _memory_rows.items():
            if t == tenant_id:
                by_doc.setdefault(row["doc_id"], []).append(row)
        out = [
            DocumentSummary(
                doc_id=doc_id,
                title=rows[0]["title"],
                source=rows[0]["source"],
                chunks=len(rows),
                created_at=rows[0]["created_at"],
                # Metadata lives on chunk 0 only, so read it from there rather than from
                # whichever row happened to sort first.
                metadata=dict(_first_chunk(rows).get("metadata") or {}),
            )
            for doc_id, rows in by_doc.items()
        ]
        out.sort(key=lambda d: (-d.created_at, d.doc_id))
        return out[:limit]

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        counts = await db.execute(
            select(
                DocumentChunk.doc_id,
                func.count(),
                func.min(DocumentChunk.created_at),
            )
            .where(DocumentChunk.tenant_id == tenant_id)
            .group_by(DocumentChunk.doc_id)
            # `doc_id` is the tiebreaker, not decoration: two documents ingested in the same
            # millisecond came back stably ordered on one arm and arbitrarily on the other,
            # while the route documents "newest first" as a promise.
            .order_by(func.min(DocumentChunk.created_at).desc(), DocumentChunk.doc_id)
            .limit(limit)
        )
        grouped = counts.all()
        if not grouped:
            return []

        # Title, source and metadata all live on chunk 0, and none of them can be aggregated:
        # `max(jsonb)` does not exist in Postgres, and `min(title)` over the group is only
        # right by accident because every chunk carries the same title. A second indexed
        # lookup of the chunk-0 rows is the honest version, and it is what makes this arm
        # return metadata at all — it previously returned none while the twin returned it.
        heads = await db.execute(
            select(
                DocumentChunk.doc_id,
                DocumentChunk.title,
                DocumentChunk.source,
                DocumentChunk.metadata_json,
            ).where(
                DocumentChunk.tenant_id == tenant_id,
                DocumentChunk.chunk_index == 0,
                DocumentChunk.doc_id.in_([r[0] for r in grouped]),
            )
        )
        head = {r[0]: r for r in heads.all()}
        return [
            DocumentSummary(
                doc_id=doc_id,
                title=(head.get(doc_id) or (None, "", "", {}))[1] or "",
                source=(head.get(doc_id) or (None, "", "", {}))[2] or "",
                chunks=count,
                created_at=created or 0,
                metadata=dict((head.get(doc_id) or (None, "", "", {}))[3] or {}),
            )
            for doc_id, count, created in grouped
        ]


async def delete_document(settings: Settings, tenant_id: str, doc_id: str) -> int:
    """Remove every chunk of one document. Returns how many were removed."""
    if _use_memory(settings):
        keys = [k for k, r in _memory_rows.items() if k[0] == tenant_id and r["doc_id"] == doc_id]
        for key in keys:
            _memory_rows.pop(key, None)
        return len(keys)

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        result = await db.execute(
            delete(DocumentChunk).where(DocumentChunk.tenant_id == tenant_id, DocumentChunk.doc_id == doc_id)
        )
        await db.commit()
        return int(getattr(result, "rowcount", 0) or 0)


async def count_documents(settings: Settings, tenant_id: str) -> int:
    if _use_memory(settings):
        return len({r["doc_id"] for (t, _), r in _memory_rows.items() if t == tenant_id})
    factory = get_session_factory(settings=settings)
    async with factory() as db:
        result = await db.execute(
            select(func.count(distinct(DocumentChunk.doc_id))).where(DocumentChunk.tenant_id == tenant_id)
        )
        return int(result.scalar() or 0)


async def search_documents(
    settings: Settings,
    *,
    tenant_id: str,
    query: str,
    limit: int = 5,
    embedder: Any | None = None,
) -> list[DocumentHit]:
    """Hybrid retrieval over the corpus. Channels that cannot run are skipped, not faked."""
    query = (query or "").strip()
    if not query:
        return []

    vector: list[float] | None = None
    if embedder is not None and getattr(embedder, "enabled", False):
        try:
            produced = await embedder.embed([query])
            vector = list(produced[0]) if produced else None
        except Exception:
            logger.warning("query embedding failed; lexical channel only", exc_info=True)

    if _use_memory(settings):
        ranked, rows = _channels_in_memory(tenant_id, query, vector)
    else:
        ranked, rows = await _channels_in_postgres(settings, tenant_id, query, vector)

    fused = rrf_fuse(ranked)
    ordered = sorted(fused.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    hits: list[DocumentHit] = []
    for chunk_id, score in ordered:
        row = rows.get(chunk_id)
        if row is None:
            continue
        hits.append(
            DocumentHit(
                doc_id=row["doc_id"],
                chunk_id=chunk_id,
                chunk_index=row["chunk_index"],
                title=row["title"],
                source=row["source"],
                content=row["content"],
                score=score,
                channels=tuple(name for name, ids in ranked.items() if chunk_id in ids),
            )
        )
    return hits


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def _channels_in_memory(
    tenant_id: str, query: str, vector: list[float] | None
) -> tuple[dict[str, list[str]], dict[str, dict[str, Any]]]:
    rows = {row["id"]: row for (t, _), row in _memory_rows.items() if t == tenant_id}
    q = _tokens(query)

    lexical = sorted(
        (r for r in rows.values() if q & _tokens(r["content"])),
        key=lambda r: (-len(q & _tokens(r["content"])), r["id"]),
    )
    ranked: dict[str, list[str]] = {"lexical": [r["id"] for r in lexical[:CHANNEL_DEPTH]]}

    if vector is not None:
        scored = [(r["id"], _cosine(vector, r.get("_vector") or [])) for r in rows.values()]
        scored = [(i, s) for i, s in scored if s > 0]
        scored.sort(key=lambda kv: (-kv[1], kv[0]))
        if scored:
            ranked["vector"] = [i for i, _ in scored[:CHANNEL_DEPTH]]
    return ranked, rows


def _tsquery_or(query: str) -> str | None:
    terms = [t for t in _tokens(query)]
    return " | ".join(sorted(terms)) if terms else None


async def _channels_in_postgres(
    settings: Settings, tenant_id: str, query: str, vector: list[float] | None
) -> tuple[dict[str, list[str]], dict[str, dict[str, Any]]]:
    from sqlalchemy import text as sql_text

    ranked: dict[str, list[str]] = {}
    rows: dict[str, dict[str, Any]] = {}
    factory = get_session_factory(settings=settings)
    async with factory() as db:
        tsquery = _tsquery_or(query)
        if tsquery:
            try:
                result = await db.execute(
                    sql_text(
                        f"SELECT {_HIT_COLUMNS} FROM document_chunks "
                        "WHERE tenant_id = :t AND content_tsv @@ to_tsquery('english', :q) "
                        "ORDER BY ts_rank(content_tsv, to_tsquery('english', :q)) DESC, id "
                        "LIMIT :n"
                    ),
                    {"t": tenant_id, "q": tsquery, "n": CHANNEL_DEPTH},
                )
                ids = []
                for r in result.mappings().all():
                    rows[r["id"]] = dict(r)
                    ids.append(r["id"])
                ranked["lexical"] = ids
            except Exception:
                # Guarded like the vector channel below, for the reason `memory/recall.py`
                # gives: the generated column only exists after `0010`, so a pod running ahead
                # of the migration — or a statement timeout — should lose a channel, not the
                # request. Unguarded, this took the vector channel down with it as well.
                logger.warning("lexical channel failed; vector only", exc_info=True)

        if vector is not None:
            literal = "[" + ",".join(f"{float(x):.7g}" for x in vector) + "]"
            try:
                result = await db.execute(
                    sql_text(
                        f"SELECT {_HIT_COLUMNS} FROM document_chunks "
                        "WHERE tenant_id = :t AND embedding IS NOT NULL "
                        "ORDER BY embedding <=> CAST(:v AS vector), id LIMIT :n"
                    ),
                    {"t": tenant_id, "v": literal, "n": CHANNEL_DEPTH},
                )
                ids = []
                for r in result.mappings().all():
                    rows[r["id"]] = dict(r)
                    ids.append(r["id"])
                if ids:
                    ranked["vector"] = ids
            except Exception:
                # A dimension mismatch or a missing extension costs the vector channel, not
                # the search — the lexical channel above has already run.
                logger.warning("vector channel failed; lexical only", exc_info=True)
    return ranked, rows


__all__ = [
    "CHANNEL_DEPTH",
    "MAX_CHUNKS_PER_DOC",
    "DocumentHit",
    "DocumentSummary",
    "count_documents",
    "delete_document",
    "document_id",
    "list_documents",
    "put_document",
    "reset_documents_for_tests",
    "search_documents",
]
