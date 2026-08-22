"""Optional text embeddings (``felix-harness[embeddings]``) with cosine rank helpers."""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence

logger = logging.getLogger("felix.embeddings")

_models: dict[str, object] = {}


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
    """Embed texts with sentence-transformers. Returns None if the extra is missing."""
    if not texts:
        return []
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        return None
    try:
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


__all__ = ["cosine_similarity", "encode_texts", "rank_indices_by_query"]
