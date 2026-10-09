"""Every thread `GET /chat/sessions` lists for a tenant, across all its pages.

The listing is paged, and a conformance database is shared by every test that ever wrote to its
tenant, so a thread a test just wrote need not be on the first page. A check that looked at one
page would pass a "not listed" assertion by never reaching the page the thread is on.
"""

from __future__ import annotations

from typing import Any


async def every_listed(settings: Any, tenant_id: str, *, limit: int = 500) -> list[dict[str, Any]]:
    from felix.session.thread_state import list_thread_metadata

    rows: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        page, cursor = await list_thread_metadata(
            settings=settings, tenant_id=tenant_id, limit=limit, cursor=cursor
        )
        rows.extend(page)
        if cursor is None:
            return rows
