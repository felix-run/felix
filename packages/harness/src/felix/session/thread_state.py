"""Persist thread leaf pointers (memory + Postgres thread_state)."""

from __future__ import annotations

import time
from typing import Any

from felix.config import Settings
from felix.session.tree import get_leaf as _mem_get_leaf
from felix.session.tree import set_leaf as _mem_set_leaf


async def persist_leaf(
    *,
    settings: Settings | None,
    tenant_id: str,
    thread_id: str,
    leaf_event_id: str | None,
) -> None:
    """Update in-memory leaf and optionally Postgres thread_state."""
    _mem_set_leaf(thread_id, leaf_event_id)
    if settings is None:
        return
    url = settings.database_url
    if ":memory:" in url or "sqlite" in url or url.startswith("memory://"):
        return
    from felix.db.models import ThreadState
    from felix.db.session import get_session_factory

    now = int(time.time())
    factory = get_session_factory(settings=settings)
    async with factory() as db:
        row = await db.get(ThreadState, (tenant_id, thread_id))
        if row is None:
            db.add(
                ThreadState(
                    tenant_id=tenant_id,
                    thread_id=thread_id,
                    leaf_event_id=leaf_event_id,
                    labels_json={},
                    updated_at=now,
                )
            )
        else:
            row.leaf_event_id = leaf_event_id
            row.updated_at = now
        await db.commit()


async def load_leaf(
    *,
    settings: Settings | None,
    tenant_id: str,
    thread_id: str,
) -> str | None:
    mem = _mem_get_leaf(thread_id)
    if mem is not None:
        return mem
    if settings is None:
        return None
    url = settings.database_url
    if ":memory:" in url or "sqlite" in url or url.startswith("memory://"):
        return None
    from felix.db.models import ThreadState
    from felix.db.session import get_session_factory

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        row = await db.get(ThreadState, (tenant_id, thread_id))
        if row and row.leaf_event_id:
            _mem_set_leaf(thread_id, row.leaf_event_id)
            return row.leaf_event_id
    return None


__all__ = ["load_leaf", "persist_leaf"]
