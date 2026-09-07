"""Session full-text search over event content (Postgres tsvector / in-memory)."""

from __future__ import annotations

import logging
from typing import Any

from felix.config import Settings

logger = logging.getLogger("felix.session.search")

# In-memory index for unit tests: tenant -> list of {thread_id, seq, content, event_id}
_memory_index: list[dict[str, Any]] = []


def index_event_memory(
    *,
    tenant_id: str,
    thread_id: str,
    seq: int,
    content: str | None,
    event_id: str | None = None,
) -> None:
    if not content:
        return
    _memory_index.append(
        {
            "tenant_id": tenant_id,
            "thread_id": thread_id,
            "seq": seq,
            "content": content,
            "event_id": event_id,
        }
    )


def drop_thread_index(*, tenant_id: str, thread_id: str) -> int:
    """Forget everything indexed for one thread. Returns how many entries went.

    The index is a second copy of event content, so it has to be deleted wherever the events
    are. On Postgres that is free — `content_tsv` is a generated column and goes with the row
    — which is exactly why this is easy to miss on the twin: adding the writer without this
    turns `DELETE /chat/history/{id}` into a delete that leaves the text findable, and lets
    the re-used `seq` numbers point a client at the wrong event.
    """
    before = len(_memory_index)
    # Filtered in place rather than rebound: the list is a module global, and anything holding
    # a reference to it — a test, a future reader — would keep the pre-delete copy alive and
    # disagree with `search_sessions` about what the index contains.
    _memory_index[:] = [
        row for row in _memory_index if not (row["tenant_id"] == tenant_id and row["thread_id"] == thread_id)
    ]
    return before - len(_memory_index)


def reset_search_index_for_tests() -> None:
    _memory_index.clear()


def _use_memory(settings: Settings) -> bool:
    url = settings.database_url
    return ":memory:" in url or "sqlite" in url or url.startswith("memory://")


async def search_sessions(
    settings: Settings,
    tenant_id: str,
    query: str,
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Search session event content. Uses Postgres FTS when available."""
    q = (query or "").strip()
    if not q:
        return []
    limit = max(1, min(int(limit), 100))

    if _use_memory(settings):
        needle = q.lower()
        hits = [
            e
            for e in _memory_index
            if e["tenant_id"] == tenant_id and needle in (e.get("content") or "").lower()
        ]
        return hits[:limit]

    try:
        from sqlalchemy import text

        from felix.db.session import get_session_factory

        factory = get_session_factory(settings=settings)
        async with factory() as db:
            # Prefer generated tsvector column when migration applied; fall back to ILIKE.
            try:
                rows = (
                    (
                        await db.execute(
                            text(
                                """
                            SELECT tenant_id, thread_id, seq, content,
                                   event_metadata->>'event_id' AS event_id,
                                   ts_rank(content_tsv, plainto_tsquery('english', :q)) AS rank
                            FROM session_events
                            WHERE tenant_id = :tenant
                              AND content_tsv @@ plainto_tsquery('english', :q)
                            ORDER BY rank DESC
                            LIMIT :lim
                            """
                            ),
                            {"tenant": tenant_id, "q": q, "lim": limit},
                        )
                    )
                    .mappings()
                    .all()
                )
                return [dict(r) for r in rows]
            except Exception:
                logger.debug("fts unavailable; falling back to ILIKE", exc_info=True)
                rows = (
                    (
                        await db.execute(
                            text(
                                """
                            SELECT tenant_id, thread_id, seq, content,
                                   event_metadata->>'event_id' AS event_id
                            FROM session_events
                            WHERE tenant_id = :tenant
                              AND content ILIKE :pat
                            ORDER BY seq DESC
                            LIMIT :lim
                            """
                            ),
                            {"tenant": tenant_id, "pat": f"%{q}%", "lim": limit},
                        )
                    )
                    .mappings()
                    .all()
                )
                return [dict(r) for r in rows]
    except Exception:
        logger.debug("session search failed", exc_info=True)
        return []


__all__ = [
    "drop_thread_index",
    "index_event_memory",
    "reset_search_index_for_tests",
    "search_sessions",
]
