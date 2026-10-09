"""Make an HNSW-ordered vector query return the rows it was asked for.

Memory and document recall order by `embedding <=> :v` within one tenant, and for a tenant holding
most of a table's rows the planner serves that order from the table's HNSW index. That index is
global, and an HNSW scan visits `hnsw.ef_search` candidates (40 by default) before the tenant
filter runs, so the query got only the candidates that happened to be the tenant's: on synthetic
clustered vectors (pgvector 0.8, 100k rows), document recall asked for 40 and got 35. A smaller
tenant whose filter the planner still served from the index fared far worse. Iterative scan
(pgvector >= 0.8) keeps scanning until the `LIMIT` is met; `strict_order` keeps the index order
exact, which the tiebreakers sorted on top of it rely on.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("felix.db.vector")

_warned_unsupported = False

#: The statement, a constant so a test can stand in a parameter the server refuses, as pgvector
#: before 0.8 refuses this one.
ITERATIVE_SCAN = "SET LOCAL hnsw.iterative_scan = strict_order"


async def scan_until_limit(db: AsyncSession) -> bool:
    """Turn on iterative HNSW scans for the rest of ``db``'s transaction; whether it could.

    `SET LOCAL`, so it ends with the transaction and never outlives it on a pooled connection.
    In a savepoint of its own: pgvector before 0.8 refuses the parameter once it has loaded in the
    session, and that refusal would otherwise abort the transaction the recall query is about to
    run in. There the query runs as it always has, and this says so once per process. (Before the
    library loads, Postgres takes the name as an inert placeholder and refuses nothing; the query
    then runs as it always has too.)
    """
    global _warned_unsupported
    from sqlalchemy import text

    try:
        async with db.begin_nested():
            await db.execute(text(ITERATIVE_SCAN))
    except Exception:
        if not _warned_unsupported:
            _warned_unsupported = True
            logger.warning(
                "hnsw.iterative_scan is unavailable (pgvector < 0.8?): vector recall for a large "
                "tenant may return fewer rows than it asked for",
                exc_info=True,
            )
        return False
    return True


__all__ = ["scan_until_limit"]
