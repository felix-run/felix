"""Walk `GET /chat/sessions`'s store pages for a tenant -- every page, or every row across them.

The listing is paged, and a conformance database is shared by every test that ever wrote to its
tenant, so a thread a test just wrote need not be on the first page. A check that looked at one
page would pass a "not listed" assertion by never reaching the page the thread is on.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any


async def listed_pages(
    settings: Any, tenant_id: str, *, limit: int = 500, max_pages: int = 100
) -> AsyncIterator[list[dict[str, Any]]]:
    """Each page in turn. Fails on a cursor that does not advance, rather than spinning to the
    suite's timeout in whichever test happened to list threads."""
    from felix.session.thread_state import list_thread_metadata

    seen: set[str] = set()
    cursor: str | None = None
    for _ in range(max_pages):
        page, cursor = await list_thread_metadata(
            settings=settings, tenant_id=tenant_id, limit=limit, cursor=cursor
        )
        yield page
        if cursor is None:
            return
        assert cursor not in seen, f"the cursor did not advance: {cursor!r}"
        seen.add(cursor)
    raise AssertionError(f"still paging after {max_pages} pages")


async def every_listed(settings: Any, tenant_id: str) -> list[dict[str, Any]]:
    return [row async for page in listed_pages(settings, tenant_id) for row in page]
