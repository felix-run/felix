"""Embed memories that were stored without a vector.

Capture stores a fact without a vector whenever the embedder is unset or down
(`capture._embed_facts` never raises), and nothing ever came back for it: a deployment that
turned an embedder on found every memory written before that day invisible to the vector
channel for good. The worker's `consolidate_memory` cron calls `embed_missing` every tick, and
it embeds a bounded batch of those rows.

The store owns the reads and writes (`memory.store.missing_embeddings` / `write_embeddings`);
this module owns the decisions around them -- which embedder, whether it is worth calling, and
what to do when the column will not take what it makes. Every one of those decisions is about
not spending: an embedding endpoint charges per request, and this runs every fifteen minutes.
"""

from __future__ import annotations

import logging

from felix.config import Settings
from felix.memory import store as memory_store

logger = logging.getLogger("felix.memory.backfill")

#: Rows embedded per tick, in one embedder call.
BACKFILL_BATCH = 100

# `(FELIX_MEMORY_EMBEDDER, model)` pairs whose vectors came back the wrong width for the
# column. An embedder whose declared `dim` disagrees is refused before it is called
# (`embed_missing`), but a declared `dim` is not a measured one: every built-in backend reports
# `FELIX_MEMORY_EMBEDDING_DIM` (768 by default) whatever its model actually makes, so a 1536-d
# model passes the pre-check and is only found out by its first batch. Hence this, for every
# embedder rather than only those declaring no `dim`. Remembered for the life of the process:
# the refused rows stay vectorless, so each tick would otherwise select and pay for the same
# batch again. A configuration change means a restart, which clears it.
_refused: set[tuple[str, str]] = set()


def reset_backfill_for_tests() -> None:
    _refused.clear()


def _refuse_width(key: tuple[str, str], width: int, expected: int) -> None:
    if key not in _refused:
        logger.warning(
            "embedding backfill: %s makes %d-dimensional vectors but memory_vectors.embedding is "
            "vector(%d); not backfilling. Set FELIX_MEMORY_EMBEDDING_MODEL to a model of the "
            "right size, or rebuild the column.",
            key[1] or key[0],
            width,
            expected,
        )
    _refused.add(key)


async def embed_missing(settings: Settings, *, limit: int = BACKFILL_BATCH) -> int:
    """Embed up to `limit` active memories stored without a vector. Returns rows embedded.

    One embedder call per tick, for the whole batch, as capture does. Never raises -- the
    embedder this exists to recover from may still be down, and the next tick retries.

    The width is checked before the embedder is called wherever it can be: an embedder that
    declares a `dim` the column disagrees with costs nothing. One that declares none, or
    declares the wrong one, is found out by its first batch and not called again (`_refused`).
    """
    from felix.memory.embedder import build_embedder

    try:
        embedder = build_embedder(settings)
        if not embedder.enabled:
            return 0
        key = (str(settings.memory_embedder or ""), embedder.model)
        if key in _refused:
            return 0
        expected = await memory_store.configured_embedding_dim(settings)
        if expected is not None and embedder.dim is not None and embedder.dim != expected:
            _refuse_width(key, embedder.dim, expected)
            return 0
        batch = await memory_store.missing_embeddings(settings, limit=limit)
        if not batch:
            return 0
        vectors = await embedder.embed([content for _, _, content in batch])
        if len(vectors) != len(batch):
            # Matched to rows by position, so a short batch would give one memory another's
            # meaning. Dropped whole; the next tick asks again.
            logger.warning(
                "embedding backfill: %d vectors for %d memories; skipped", len(vectors), len(batch)
            )
            return 0
        if expected is not None and vectors and len(vectors[0]) != expected:
            _refuse_width(key, len(vectors[0]), expected)
            return 0
        return await memory_store.write_embeddings(
            settings, batch, vectors, model=embedder.model, expected_dim=expected
        )
    except Exception:
        logger.warning(
            "embedding backfill failed; memories stay unembedded until the next tick", exc_info=True
        )
        return 0


__all__ = ["BACKFILL_BATCH", "embed_missing", "reset_backfill_for_tests"]
