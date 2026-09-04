"""The document corpus: ingest, search, inspect and remove what an agent can retrieve.

The operator-facing half of `felix/documents/`. An agent answering from a corpus is only as
trustworthy as the corpus, so whoever runs it needs to see what is in there, find the chunk
behind a bad answer, and remove it — without a database console. That is the same argument
`/memory` makes, and this follows its shape deliberately.

Reads are gated by `documents:read`, mutations by `documents:write`, which implies the read
scope through the usual rule. The tenant always comes from the authenticated principal and
never from the request body, so one tenant cannot ingest into — or search — another's corpus.

`GET /documents/search` returns chunks with their channel attribution, because when a
retrieval is wrong the first question is which retriever produced it.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from felix.auth.mgmt import (
    SCOPE_DOCUMENTS_READ,
    SCOPE_DOCUMENTS_WRITE,
    require_mgmt_scopes,
    tenant_id_from_request,
)
from felix.documents.chunking import DEFAULT_MAX_CHARS, DEFAULT_OVERLAP_CHARS
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

router = APIRouter(tags=["Documents"])

# Bounded because a corpus is stored, indexed and later injected into prompts.
#
# Kept **below** `CORE_BODY_LIMIT_BYTES` (1 MiB) deliberately. Set above it, this field
# advertises a size the server will never accept: the body-limit middleware answers 413
# before the route is reached, so the caller is told the request is too large without being
# told what the actual document ceiling is. A JSON envelope and escaping also consume part of
# that budget, hence the margin rather than the exact figure.
#
# `MAX_CHUNKS_PER_DOC` in the store bounds what one document *becomes*; this bounds what one
# request may *carry*. Different failures, different owners.
MAX_DOCUMENT_CHARS = 750_000
MAX_TITLE_CHARS = 400
MAX_SOURCE_CHARS = 2_000


# Serialized ceiling on `metadata`. It was the one field in this model with no bound, and it
# is written per document but *travels* with a request that also carries up to
# `MAX_CHUNKS_PER_DOC` chunks — measured at 1.7 GB stored from a single request inside the
# body limit before metadata stopped being copied onto every row. Bounded here as well as
# denormalised away in the store, because either fix alone leaves the other shape available.
MAX_METADATA_BYTES = 16_384


def _no_control_chars(value: str, field: str, *, allow_newlines: bool) -> str:
    """Reject NUL and friends.

    A NUL is valid JSON and passes every length check, and psycopg refuses it client-side with
    `DataError` — not `ValueError` — so it escaped the handler's 400 and became an uncaught
    500. The in-memory arm accepts it happily, so the conformance contract cannot see the
    divergence either.

    `title` and `source` additionally forbid newlines, because they are single-line values by
    nature and both reach line-oriented formats: `source` is logged on an embedding failure,
    and both are rendered per hit. `%r` escapes a newline today, which is exactly the kind of
    protection that disappears when someone changes it to `%s` — so the newline is refused at
    the boundary instead, the same way the search tool flattens what it renders.
    """
    allowed = "\t\n\r" if allow_newlines else "\t"
    bad = {ch for ch in value if ch == "\x00" or (ord(ch) < 32 and ch not in allowed)}
    if bad:
        kind = "control characters" if allow_newlines else "control characters or newlines"
        raise ValueError(f"{field} contains {kind} that cannot be stored")
    return value


class DocumentIngestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=MAX_TITLE_CHARS)
    # Where the text came from. Opaque to the harness, shown with every hit, and half of the
    # document's identity — re-ingesting the same (source, title) replaces rather than
    # duplicates, which is what makes a re-sync idempotent.
    source: str = Field(default="", max_length=MAX_SOURCE_CHARS)
    text: str = Field(min_length=1, max_length=MAX_DOCUMENT_CHARS)
    metadata: dict[str, Any] = Field(default_factory=dict)
    max_chars: int = Field(default=DEFAULT_MAX_CHARS, ge=128, le=20_000)
    overlap_chars: int = Field(default=DEFAULT_OVERLAP_CHARS, ge=0, le=4_000)

    @field_validator("title", "source")
    @classmethod
    def _single_line(cls, v: str, info: ValidationInfo) -> str:
        return _no_control_chars(v, str(info.field_name), allow_newlines=False)

    @field_validator("text")
    @classmethod
    def _printable(cls, v: str, info: ValidationInfo) -> str:
        return _no_control_chars(v, str(info.field_name), allow_newlines=True)

    @field_validator("metadata")
    @classmethod
    def _bounded_metadata(cls, v: dict[str, Any]) -> dict[str, Any]:
        import json

        try:
            size = len(json.dumps(v).encode())
        except (TypeError, ValueError) as exc:
            raise ValueError("metadata must be JSON-serialisable") from exc
        if size > MAX_METADATA_BYTES:
            raise ValueError(f"metadata is {size} bytes; the ceiling is {MAX_METADATA_BYTES}")
        return v


def _embedder(request: Request) -> Any:
    """The configured embedder, or None.

    Built per request rather than cached on app state because `build_embedder` reads settings
    and the null case is free; a corpus ingested with no embedder is text-searchable, which is
    the documented default rather than a degraded mode.
    """
    from felix.memory.embedder import build_embedder

    try:
        return build_embedder(request.app.state.settings)
    except Exception:  # pragma: no cover - a broken embedder must not break ingest
        return None


@router.get("")
@router.get("/")
async def list_documents(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    """Documents in this tenant's corpus, newest first."""
    from felix.documents import store as doc_store

    require_mgmt_scopes(request, SCOPE_DOCUMENTS_READ)
    items = await doc_store.list_documents(
        request.app.state.settings, tenant_id_from_request(request), limit=limit
    )
    return {
        "items": [
            {
                "doc_id": d.doc_id,
                "title": d.title,
                "source": d.source,
                "chunks": d.chunks,
                "created_at": d.created_at,
            }
            for d in items
        ],
        "count": len(items),
    }


@router.get("/search")
async def search_documents(
    request: Request,
    q: str = Query(min_length=1, max_length=1_000),
    limit: int = Query(default=5, ge=1, le=50),
) -> dict[str, Any]:
    """Hybrid retrieval over the corpus, with the channels that surfaced each hit.

    `channels` is the operator's answer to "why did it return this": `lexical` alone means the
    vector channel did not run — usually `FELIX_MEMORY_EMBEDDER=none`, the default — rather
    than that it ran and disagreed.
    """
    from felix.documents import store as doc_store

    require_mgmt_scopes(request, SCOPE_DOCUMENTS_READ)
    hits = await doc_store.search_documents(
        request.app.state.settings,
        tenant_id=tenant_id_from_request(request),
        query=q,
        limit=limit,
        embedder=_embedder(request),
    )
    return {
        "items": [
            {
                "doc_id": h.doc_id,
                "chunk_id": h.chunk_id,
                "chunk_index": h.chunk_index,
                "title": h.title,
                "source": h.source,
                "content": h.content,
                "score": h.score,
                "channels": list(h.channels),
            }
            for h in hits
        ],
        "count": len(hits),
    }


@router.post("")
@router.post("/")
async def ingest_document(request: Request, body: DocumentIngestRequest) -> dict[str, Any]:
    """Ingest or replace one document. Idempotent on `(source, title)`."""
    from felix.documents import store as doc_store

    require_mgmt_scopes(request, SCOPE_DOCUMENTS_WRITE)
    settings = request.app.state.settings
    tenant_id = tenant_id_from_request(request)

    # Checked before the work, and only for a document that does not already exist — a
    # re-ingest of something already in the corpus must not be refused for being over a
    # ceiling it is not adding to. `count_documents` existed, was tested, and was called by
    # nothing until this; a quota is what it was for.
    ceiling = int(getattr(settings, "documents_max_per_tenant", 0) or 0)
    if ceiling:
        doc_id = doc_store.document_id(body.source, body.title)
        known = {d.doc_id for d in await doc_store.list_documents(settings, tenant_id, limit=ceiling)}
        if doc_id not in known and await doc_store.count_documents(settings, tenant_id) >= ceiling:
            raise HTTPException(
                status_code=409,
                detail=f"corpus is at its ceiling of {ceiling} documents (FELIX_DOCUMENTS_MAX_PER_TENANT)",
            )

    try:
        doc_id, chunks = await doc_store.put_document(
            settings,
            tenant_id=tenant_id,
            title=body.title,
            source=body.source,
            text=body.text,
            metadata=body.metadata,
            embedder=_embedder(request),
            max_chars=body.max_chars,
            overlap_chars=body.overlap_chars,
        )
    except ValueError as exc:
        # The store's chunk ceiling. A 400 rather than a 500: the caller sent something the
        # corpus will not hold, and the message says which limit it hit.
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"doc_id": doc_id, "chunks": chunks}


@router.delete("/{doc_id}")
async def delete_document(request: Request, doc_id: str) -> dict[str, Any]:
    """Remove a document and every chunk of it."""
    from felix.documents import store as doc_store

    require_mgmt_scopes(request, SCOPE_DOCUMENTS_WRITE)
    removed = await doc_store.delete_document(
        request.app.state.settings, tenant_id_from_request(request), doc_id
    )
    if not removed:
        raise HTTPException(status_code=404, detail="document not found")
    return {"doc_id": doc_id, "removed_chunks": removed}


__all__ = ["router"]
