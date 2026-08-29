"""Hybrid recall: several weak retrievers, fused.

Recall used to be `ORDER BY created_at` — the most recent facts, whether or not they
had anything to do with the question. This runs three independent channels and fuses
their *rankings* rather than their scores:

- **full text** over `content_tsv`, which finds literal overlap;
- **topic key** over `topic_tsv`, which finds `user.timezone` from "what timezone";
- **vector** over the pgvector column, which finds paraphrase and needs an embedder.

Reciprocal Rank Fusion is what makes combining them safe. Each channel scores on its
own incomparable scale — `ts_rank_cd` against cosine distance — so any weighted sum of
scores is meaningless. RRF throws the magnitudes away and keeps only position, which
is the one thing the channels agree on the meaning of. A document ranked well by two
weak channels beats one ranked well by a single strong one, which is the behaviour
worth having when no channel is reliable alone.

Channels that cannot run are skipped, not faked: no embedder means no vector channel,
and the rest still return results.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from felix.config import Settings
from felix.db.session import _use_memory, get_session_factory
from felix.memory import store as memory_store

logger = logging.getLogger("felix.memory.recall")

# The RRF constant. 60 is the value from the original paper and the one every
# implementation uses; it flattens the contribution of very high ranks so that a
# single channel cannot dominate on its top hit alone.
RRF_K = 60

# Applied after fusion, not inside it. A stable fact is worth more at equal rank than
# a passing observation, but this must not let one channel's ordering be overridden.
KIND_WEIGHTS = {
    "fact": 1.3,
    "instruction": 1.15,
    "procedure": 1.1,
    "event": 1.0,
    "task": 1.0,
}

_WORD = re.compile(r"[a-z0-9]+")


@dataclass(slots=True)
class RecallHit:
    id: str
    content: str
    kind: str
    score: float
    topic_key: str | None = None
    importance: float = 0.5
    metadata: dict[str, Any] = field(default_factory=dict)
    # Which channels surfaced this row. Not used for ranking — it is what makes a
    # disappointing result explainable rather than mysterious.
    channels: tuple[str, ...] = ()


def rrf_fuse(ranked: dict[str, list[str]], k: int = RRF_K) -> dict[str, float]:
    """Fuse per-channel rankings into one score per id.

    ``ranked`` maps a channel name to its ids in rank order. Pure and deterministic,
    so it is tested directly rather than through a database.
    """
    fused: dict[str, float] = {}
    for ids in ranked.values():
        for position, mem_id in enumerate(ids):
            fused[mem_id] = fused.get(mem_id, 0.0) + 1.0 / (k + position + 1)
    return fused


def _channels_of(ranked: dict[str, list[str]], mem_id: str) -> tuple[str, ...]:
    return tuple(name for name, ids in ranked.items() if mem_id in ids)


def _tokens(text: str) -> set[str]:
    return {t for t in _WORD.findall((text or "").lower()) if len(t) > 2}


async def recall(
    settings: Settings,
    tenant_id: str,
    query: str,
    *,
    manifest_id: str = "",
    limit: int = 8,
    kinds: list[str] | None = None,
    embedder: Any | None = None,
) -> list[RecallHit]:
    """The most relevant active memories for ``query``."""
    query = (query or "").strip()
    if not query:
        return []

    # Over-fetch per channel so fusion has something to disagree about; a channel that
    # returns exactly `limit` gives RRF nothing to work with.
    per_channel = max(limit * 2, 10)
    ranked: dict[str, list[str]] = {}

    if _use_memory(settings):
        ranked = await _channels_in_memory(
            settings,
            tenant_id,
            query,
            manifest_id=manifest_id,
            per_channel=per_channel,
            embedder=embedder,
        )
    else:
        ranked = await _channels_in_postgres(
            settings,
            tenant_id,
            query,
            manifest_id=manifest_id,
            per_channel=per_channel,
            embedder=embedder,
        )

    live = {name: ids for name, ids in ranked.items() if ids}
    fused = rrf_fuse(live)
    if not fused:
        return []

    # One query for every candidate rather than one per candidate.
    rows = await memory_store.get_many(settings, tenant_id, list(fused))
    return _rank(fused, rows, live, kinds=kinds, limit=limit)


def _rank(
    fused: dict[str, float],
    rows: dict[str, dict[str, Any]],
    live: dict[str, list[str]],
    *,
    kinds: list[str] | None,
    limit: int,
) -> list[RecallHit]:
    """Apply per-kind and importance weighting to fused ranks, newest breaking ties."""
    hits: list[tuple[float, float, RecallHit]] = []
    for mem_id, rrf_score in fused.items():
        row = rows.get(mem_id)
        if row is None or str(row.get("status") or memory_store.ACTIVE) != memory_store.ACTIVE:
            continue
        if kinds and row.get("kind") not in kinds:
            continue
        importance = float(row.get("importance") or 0.5)
        score = rrf_score * KIND_WEIGHTS.get(str(row.get("kind") or ""), 1.0) * (0.5 + importance)
        recency = float(row.get("last_used_at") or row.get("created_at") or 0)
        hits.append(
            (
                score,
                recency,
                RecallHit(
                    id=mem_id,
                    content=str(row.get("content") or ""),
                    kind=str(row.get("kind") or "fact"),
                    score=score,
                    topic_key=row.get("topic_key"),
                    importance=importance,
                    metadata=dict(row.get("metadata") or {}),
                    channels=_channels_of(live, mem_id),
                ),
            )
        )

    hits.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [hit for _, _, hit in hits[:limit]]


# Recall runs inline in a turn and is best-effort by design, so it needs a budget of its
# own. The embedder inherits `FELIX_MODEL_TIMEOUT_SECONDS`, which is sized for a long
# generation whose failure kills the run — raising it to 600s for that reason must not also
# let a degradable recall add ten minutes to a turn. Degrade on time as well as on error.
RECALL_EMBED_BUDGET_S = 5.0


async def _embed_query(embedder: Any | None, query: str) -> list[float] | None:
    if embedder is None or not getattr(embedder, "enabled", False):
        return None
    try:
        vectors = await asyncio.wait_for(embedder.embed([query]), timeout=RECALL_EMBED_BUDGET_S)
    except TimeoutError:
        logger.warning(
            "query embedding exceeded %.0fs; recall running without the vector channel",
            RECALL_EMBED_BUDGET_S,
        )
        return None
    except Exception:
        # A recall that loses its vector channel is worse than one that keeps it, and
        # far better than a turn that fails because an embedding endpoint was down.
        logger.warning("query embedding failed; recall running without the vector channel")
        return None
    return list(vectors[0]) if vectors else None


# --- in-memory twin --------------------------------------------------------------


async def _channels_in_memory(
    settings: Settings,
    tenant_id: str,
    query: str,
    *,
    manifest_id: str,
    per_channel: int,
    embedder: Any | None,
) -> dict[str, list[str]]:
    """The same three channels, over the dict backend.

    Real implementations, not stubs: this is the path CI runs, so a channel faked here
    is a channel nothing tests.
    """
    rows = [
        row
        for (t, _), row in memory_store._memory_rows.items()
        if t == tenant_id
        and str(row.get("status") or memory_store.ACTIVE) == memory_store.ACTIVE
        and (not manifest_id or row.get("manifest_id") == manifest_id)
    ]
    q_tokens = _tokens(query)
    ranked: dict[str, list[str]] = {}

    scored = [(len(q_tokens & _tokens(str(row.get("content") or ""))), row["id"]) for row in rows]
    ranked["fts"] = [i for n, i in sorted(scored, key=lambda s: s[0], reverse=True) if n][:per_channel]

    topic_scored = [
        (len(q_tokens & _tokens(str(row.get("topic_key") or "").replace(".", " "))), row["id"])
        for row in rows
        if row.get("topic_key")
    ]
    ranked["topic"] = [i for n, i in sorted(topic_scored, key=lambda s: s[0], reverse=True) if n][
        :per_channel
    ]

    qvec = await _embed_query(embedder, query)
    if qvec is not None:
        from felix.embeddings import cosine_similarity

        vec_scored = [
            (cosine_similarity(qvec, row["embedding"]), row["id"]) for row in rows if row.get("embedding")
        ]
        ranked["vector"] = [
            i for score, i in sorted(vec_scored, key=lambda s: s[0], reverse=True) if score > 0
        ][:per_channel]
    return ranked


# --- postgres ---------------------------------------------------------------------

# Both channels OR their terms rather than ANDing them. `plainto_tsquery` and
# `websearch_to_tsquery` both AND, which means a natural-language question matches
# nothing unless every word appears — "what timezone" found no `user.timezone` at all,
# while the in-memory twin scored it on overlap and did. Recall wants breadth; RRF and
# the ranking pass are what turn breadth back into precision.
#
# The query is built from `_tokens`, which keeps only `[a-z0-9]+`, so no tsquery
# operator can survive into `to_tsquery` from user text.
_FTS_SQL = """
    SELECT id FROM memory_vectors
     WHERE tenant_id = :tenant AND status = 'active'
       AND (:manifest = '' OR manifest_id = :manifest)
       AND content_tsv @@ to_tsquery('english', :tsq)
     ORDER BY ts_rank_cd(content_tsv, to_tsquery('english', :tsq)) DESC
     LIMIT :lim
"""

_TOPIC_SQL = """
    SELECT id FROM memory_vectors
     WHERE tenant_id = :tenant AND status = 'active' AND topic_key IS NOT NULL
       AND (:manifest = '' OR manifest_id = :manifest)
       AND topic_tsv @@ to_tsquery('simple', :tsq)
     ORDER BY ts_rank(topic_tsv, to_tsquery('simple', :tsq)) DESC
     LIMIT :lim
"""


def _tsquery_or(query: str) -> str | None:
    """An OR-joined tsquery built only from alphanumeric tokens, or None."""
    tokens = sorted(_tokens(query))
    return " | ".join(tokens) if tokens else None


_VECTOR_SQL = """
    SELECT id FROM memory_vectors
     WHERE tenant_id = :tenant AND status = 'active' AND embedding IS NOT NULL
       AND (:manifest = '' OR manifest_id = :manifest)
     ORDER BY embedding <=> CAST(:vec AS vector)
     LIMIT :lim
"""


async def _channels_in_postgres(
    settings: Settings,
    tenant_id: str,
    query: str,
    *,
    manifest_id: str,
    per_channel: int,
    embedder: Any | None,
) -> dict[str, list[str]]:
    from sqlalchemy import text as sa_text

    ranked: dict[str, list[str]] = {}
    tsq = _tsquery_or(query)
    params = {"tenant": tenant_id, "manifest": manifest_id, "tsq": tsq, "lim": per_channel}
    qvec = await _embed_query(embedder, query)

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        for name, sql in (("fts", _FTS_SQL), ("topic", _TOPIC_SQL)):
            if tsq is None:
                continue
            try:
                rows = (await db.execute(sa_text(sql), params)).scalars().all()
                ranked[name] = [str(r) for r in rows]
            except Exception:
                # `websearch_to_tsquery` never raises on user punctuation, but the
                # generated columns only exist after 0009 — a deployment mid-upgrade
                # should lose a channel, not the turn.
                logger.debug("recall channel %s unavailable", name, exc_info=True)

        if qvec is not None:
            literal = "[" + ",".join(repr(float(v)) for v in qvec) + "]"
            try:
                rows = (await db.execute(sa_text(_VECTOR_SQL), {**params, "vec": literal})).scalars().all()
                ranked["vector"] = [str(r) for r in rows]
            except Exception:
                logger.debug("recall vector channel unavailable", exc_info=True)
    return ranked


__all__ = ["KIND_WEIGHTS", "RRF_K", "RecallHit", "recall", "rrf_fuse"]
