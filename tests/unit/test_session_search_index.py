"""The in-memory search index, at the seam where it is written and dropped.

The index is a second copy of session event content. It had no production writer at all until
the store began feeding it, so the properties that keep two copies honest — it is written on
append, dropped on delete, and scoped to one tenant — have never been pinned anywhere.

`tests/e2e/test_chat_sessions.py` covers the same ground through HTTP. This file covers the
part HTTP cannot reach under `auth_mode=none`: that one tenant's delete leaves another
tenant's identically-named thread alone.
"""

from __future__ import annotations

import pytest
from felix.session.search import _memory_index, drop_thread_index, search_sessions
from felix.session.store import InMemorySessionStore
from felix.session.types import AppendableEvent


def _settings():
    from felix.config import Settings

    return Settings(database_url="memory://search-index", object_store="memory")


async def _append(tenant: str, thread: str, text: str) -> None:
    store = InMemorySessionStore(tenant_id=tenant)
    await store.open(thread).append(AppendableEvent(kind="message", role="user", content=text))


@pytest.mark.asyncio
async def test_an_append_is_immediately_searchable() -> None:
    """The writer exists at all — the property that was absent for the index's whole life."""
    await _append("acme", "t1", "the zucchini marker")

    hits = await search_sessions(_settings(), "acme", "zucchini")
    assert [h["content"] for h in hits] == ["the zucchini marker"], hits


@pytest.mark.asyncio
async def test_one_tenants_delete_leaves_another_tenants_thread_alone() -> None:
    """Thread ids are namespaced per tenant, but the index is one flat process-global list.

    Dropping by `thread_id` alone would delete across the tenant boundary, and the caller
    whose data vanished would have no way to tell it had happened.
    """
    await _append("acme", "shared-name", "acme's zucchini")
    await _append("globex", "shared-name", "globex's zucchini")

    removed = drop_thread_index(tenant_id="acme", thread_id="shared-name")
    assert removed == 1

    assert await search_sessions(_settings(), "acme", "zucchini") == []
    survivors = await search_sessions(_settings(), "globex", "zucchini")
    assert [h["content"] for h in survivors] == ["globex's zucchini"], survivors


@pytest.mark.asyncio
async def test_a_reset_thread_leaves_nothing_behind_in_the_index() -> None:
    """`reset()` is what `DELETE /chat/history/{id}` and the retention sweep both reach."""
    store = InMemorySessionStore(tenant_id="acme")
    session = store.open("t1")
    await session.append(AppendableEvent(kind="message", role="user", content="findable"))
    assert await search_sessions(_settings(), "acme", "findable")

    await session.reset()

    assert await search_sessions(_settings(), "acme", "findable") == []
    assert _memory_index == [], _memory_index


@pytest.mark.asyncio
async def test_search_does_not_cross_the_tenant_boundary() -> None:
    """The query side of the same rule, so neither half can hold it alone."""
    await _append("acme", "t1", "acme's zucchini")

    assert await search_sessions(_settings(), "globex", "zucchini") == []


@pytest.mark.asyncio
async def test_the_retention_sweep_takes_the_index_with_the_thread() -> None:
    """Retention is a deletion guarantee, so a purged thread must stop being searchable.

    The sweep pops the whole session out of the store rather than calling `reset()`, so it is
    a second delete path and the index has to be dropped there too — otherwise the content
    outlives the retention window it was purged to satisfy, which is the opposite of what the
    sweep is for.
    """
    import time

    from felix.jobs.retention import run_retention_sweep
    from felix.session import thread_state

    store = InMemorySessionStore(tenant_id="acme")
    session = store.open("stale")
    long_ago = time.time() - (400 * 86400)
    await session.append(
        AppendableEvent(kind="message", role="user", content="ancient zucchini", ts=long_ago)
    )
    assert await search_sessions(_settings(), "acme", "zucchini")

    # The sweep reads the process-global registry and the thread's metadata clock; both have
    # to look old for the thread to count as idle.
    from felix.session import store as session_store

    session_store._memory_session_stores["acme"] = store
    thread_state._meta_by_thread["stale"] = {"updated_at": int(long_ago * 1000)}

    settings = _settings()
    object.__setattr__(settings, "session_retention_days", 30)
    await run_retention_sweep(settings)

    assert await search_sessions(settings, "acme", "zucchini") == []
