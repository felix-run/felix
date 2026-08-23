"""Optional text embeddings (``felix-harness[embeddings]``) with cosine rank helpers.

Encoding is CPU-bound and the first call for a model also loads it from disk, so it
must not run on the event loop. Every caller here sits on the request path — tool
retrieval, procedural recall, and the ``semantic:N`` session strategy all run while a
turn is being served — and a synchronous encode stalls every other request the worker
is handling, not just the one that asked for it. Async callers use
:func:`rank_indices_by_query_async`; the synchronous functions stay for callers that
are already off the loop.
"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
from collections.abc import Sequence

logger = logging.getLogger("felix.embeddings")

_models: dict[str, object] = {}
# Guards the load, not the encode. Encoding concurrently is fine; constructing the same
# SentenceTransformer twice is not — each is hundreds of MB, and moving encoding into a
# thread pool is exactly what makes that race reachable.
_model_lock = threading.Lock()


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b, strict=True):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def encode_texts(texts: list[str], model: str = "bge-base-en-v1.5") -> list[list[float]] | None:
    """Embed texts with sentence-transformers. Returns None if the extra is missing.

    Blocking and CPU-bound. Do not call this from a coroutine — use
    :func:`rank_indices_by_query_async`.
    """
    if not texts:
        return []
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        return None
    try:
        encoder = _models.get(model)
        if encoder is None:
            with _model_lock:
                # Re-check under the lock: a concurrent caller may have loaded it while
                # this one waited.
                encoder = _models.get(model)
                if encoder is None:
                    encoder = SentenceTransformer(model)
                    _models[model] = encoder
        vectors = encoder.encode(texts, convert_to_numpy=True)  # type: ignore[union-attr]
        return [list(map(float, row)) for row in vectors]
    except Exception:
        logger.debug("encode_texts failed for model=%s", model, exc_info=True)
        return None


def rank_indices_by_query(query: str, blobs: list[str], model: str) -> list[int] | None:
    """Return blob indices sorted by descending cosine similarity, or None to fall back."""
    if not blobs:
        return []
    encoded = encode_texts([query, *blobs], model=model)
    if encoded is None or len(encoded) != 1 + len(blobs):
        return None
    qv = encoded[0]
    return sorted(
        range(len(blobs)),
        key=lambda i: cosine_similarity(qv, encoded[i + 1]),
        reverse=True,
    )


async def rank_indices_by_query_async(query: str, blobs: list[str], model: str) -> list[int] | None:
    """:func:`rank_indices_by_query`, off the event loop.

    Returns ``None`` on any failure, exactly like the synchronous version, so callers
    keep their existing degrade-to-keyword-overlap path.
    """
    if not blobs:
        return []
    return await asyncio.to_thread(rank_indices_by_query, query, blobs, model)


__all__ = [
    "cosine_similarity",
    "encode_texts",
    "rank_indices_by_query",
    "rank_indices_by_query_async",
]
