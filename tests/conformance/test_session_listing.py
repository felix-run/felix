"""`GET /chat/sessions` pages a tenant's threads newest first, the same way on every backend.

It read the tenant's whole `thread_state` in one unordered query; now it returns a page and a
cursor (`felix.cursors`). The ways a keyset page goes wrong are all at a tie, so the seeded threads
share a second: a cursor that compared the second alone would skip the rest of a tie, an id order
under the database collation would put `B` and `a` the other way round from the twin, and a twin
that ordered on its millisecond stamp would split a second Postgres cannot see inside.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

BACKENDS = ["memory", "postgres"]
parametrized = pytest.mark.parametrize("store_settings", BACKENDS, indirect=True)

# (suffix, second, millisecond within it). `B` sorts before `a` byte for byte and after it under
# most collations. Within second 1000, the twin's millisecond stamps run against the id order, so
# ordering on them instead of the second would put `a` first.
SEEDED = [
    ("a", 1000, 900),
    ("B", 1000, 500),
    ("c", 1000, 100),
    ("d", 2000, 0),
    ("e", 500, 0),
]
# Newest second first, then the id compared byte for byte, descending.
EXPECTED = ["d", "c", "a", "B", "e"]


async def _seed(settings: Any, tenant: str) -> None:
    for suffix, second, ms in SEEDED:
        thread = f"{tenant}:{suffix}"
        labels = {"created_at": second * 1000, "updated_at": second * 1000 + ms, "session_name": suffix}
        if settings.database_url.startswith("memory://"):
            from felix.session import thread_state

            thread_state._meta_by_thread[thread] = {**thread_state._default_meta(), **labels}
            continue
        from felix.db.models import ThreadState
        from felix.db.session import tenant_session

        async with tenant_session(settings, tenant) as db:
            db.add(ThreadState(tenant_id=tenant, thread_id=thread, labels_json=labels, updated_at=second))
            await db.commit()


async def _pages(settings: Any, tenant: str, limit: int) -> list[list[str]]:
    from felix.session.thread_state import list_thread_metadata

    pages: list[list[str]] = []
    cursor: str | None = None
    while True:
        rows, cursor = await list_thread_metadata(
            settings=settings, tenant_id=tenant, limit=limit, cursor=cursor
        )
        pages.append([str(r["id"]).removeprefix(f"{tenant}:") for r in rows])
        if cursor is None:
            return pages
        assert len(pages) <= len(SEEDED), "the cursor never ran out"


@parametrized
@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [1, 2, 5, 6])
async def test_pages_list_every_thread_once_newest_first(store_settings: Any, limit: int) -> None:
    tenant = f"page{uuid.uuid4().hex[:12]}"
    await _seed(store_settings, tenant)

    pages = await _pages(store_settings, tenant, limit)

    assert [suffix for page in pages for suffix in page] == EXPECTED
    assert all(len(page) == limit for page in pages[:-1])
    # A last page that is exactly full says so, rather than sending the caller for an empty one.
    assert pages[-1], pages


@parametrized
@pytest.mark.asyncio
async def test_a_page_carries_the_rows_own_timestamps(store_settings: Any) -> None:
    """The cursor orders on the second; the row still reports the metadata's millisecond stamp."""
    from felix.session.thread_state import list_thread_metadata

    tenant = f"page{uuid.uuid4().hex[:12]}"
    await _seed(store_settings, tenant)

    rows, _ = await list_thread_metadata(settings=store_settings, tenant_id=tenant, limit=3)

    assert [(r["sessionName"], r["updatedAt"]) for r in rows] == [
        ("d", 2_000_000),
        ("c", 1_000_100),
        ("a", 1_000_900),
    ]


@parametrized
@pytest.mark.asyncio
async def test_a_malformed_cursor_is_refused_as_one(store_settings: Any) -> None:
    from felix.cursors import InvalidCursor
    from felix.session.thread_state import list_thread_metadata

    with pytest.raises(InvalidCursor):
        await list_thread_metadata(settings=store_settings, tenant_id="acme", limit=5, cursor="not-a-cursor")
