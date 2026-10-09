"""Make an HNSW-ordered vector query return the rows it was asked for.

Memory and document recall order by `embedding <=> :v` within one tenant, and for a tenant holding
most of a table's rows the planner serves that order from the table's HNSW index. That index is
global, and an HNSW scan visits `hnsw.ef_search` candidates (40 by default) before the tenant
filter runs, so the query kept only the candidates that happened to be the tenant's and could come
back short. Iterative scan (pgvector >= 0.8) keeps scanning until the `LIMIT` is met or
`hnsw.max_scan_tuples` (20,000 by default) are visited. `strict_order`, because the tiebreakers
are an Incremental Sort that trusts the index's order; `relaxed_order` would let them sort and cut
rows that arrived out of it.
"""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("felix.db.vector")

_STATEMENT = "SET LOCAL hnsw.iterative_scan = strict_order"

# What the server answers a parameter it does not have, or a value it does not take: pgvector
# before 0.8, once loaded in the session (Postgres 15+ reserves the `hnsw.` prefix for it).
_UNSUPPORTED = {"42704", "22023"}

_warned_unsupported = False


async def enable_iterative_hnsw_scan(db: AsyncSession) -> None:
    """Turn on iterative HNSW scans for the rest of ``db``'s transaction, where the server has them.

    `SET LOCAL`, so it ends with the transaction and never reaches the next user of a pooled
    connection. In a savepoint of its own, so a refusal costs the setting and not the transaction
    the recall query runs in next: the query then runs as it always has. Only the server's refusal
    of the parameter is blamed on the pgvector version, once per process; anything else -- a
    transaction an earlier statement already aborted, a dropped connection -- is not this
    function's to diagnose, and the query that follows meets it again.

    Before pgvector loads in a session, Postgres takes the name as a placeholder and refuses
    nothing; pgvector 0.8 adopts the value when it loads, and an older one drops it.
    """
    global _warned_unsupported
    try:
        async with db.begin_nested():
            await db.execute(text(_STATEMENT))
    except Exception as exc:
        sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
        if sqlstate not in _UNSUPPORTED:
            logger.debug("iterative HNSW scan not set", exc_info=True)
        elif not _warned_unsupported:
            _warned_unsupported = True
            logger.warning(
                "hnsw.iterative_scan is unavailable (pgvector < 0.8): vector recall for a large "
                "tenant may return fewer rows than it asked for"
            )


__all__ = ["enable_iterative_hnsw_scan"]
