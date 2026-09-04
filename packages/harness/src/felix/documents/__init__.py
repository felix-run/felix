"""Operator-ingested documents an agent can search.

Distinct from `felix/memory/`, which stores facts an *agent* wrote: different lifecycle,
different trust, different write path. Shares the retrieval machinery — `rrf_fuse` — rather
than the storage.
"""

from felix.documents.chunking import Chunk, chunk_text
from felix.documents.store import (
    DocumentHit,
    DocumentSummary,
    count_documents,
    delete_document,
    document_id,
    list_documents,
    put_document,
    reset_documents_for_tests,
    search_documents,
)

__all__ = [
    "Chunk",
    "DocumentHit",
    "DocumentSummary",
    "chunk_text",
    "count_documents",
    "delete_document",
    "document_id",
    "list_documents",
    "put_document",
    "reset_documents_for_tests",
    "search_documents",
]
