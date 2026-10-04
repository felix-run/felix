"""Reading a thread does not create it, on either backend.

`list_thread_metadata` is what `GET /chat/sessions` returns. On Postgres it lists
`thread_state` rows, and only a write inserts one. On `memory://` it lists the keys of
`thread_state._meta_by_thread`, and `get_thread_meta` used to get-or-create that entry --
so a snapshot, a lease, or any other read of an unknown thread id made that id show up as
an empty session with nothing in it but its id. The twin listed sessions Postgres never
had.

Each read entry point a route reaches is called here on a thread nobody wrote, then the
listing must not contain it. The last case is the positive control: a write lists the
thread on both arms, so the lister is not trivially empty.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

BACKENDS = ["memory", "postgres"]
TENANT = "conformance"

parametrized = pytest.mark.parametrize("store_settings", BACKENDS, indirect=True)


def _unknown_thread() -> str:
    return f"{TENANT}:{uuid.uuid4().hex}"


async def _listed(settings: Any) -> set[str]:
    from felix.session.thread_state import list_thread_metadata

    return {str(m["id"]) for m in await list_thread_metadata(settings=settings, tenant_id=TENANT)}


async def _get_meta(settings: Any, thread: str) -> None:
    from felix.session.thread_state import get_thread_meta

    meta = await get_thread_meta(settings=settings, tenant_id=TENANT, thread_id=thread)
    # The read still answers: defaults, the same shape a stored thread has.
    assert meta["phase"] == "idle"


async def _load_leaf(settings: Any, thread: str) -> None:
    from felix.session.thread_state import load_leaf

    assert await load_leaf(settings=settings, tenant_id=TENANT, thread_id=thread) is None


async def _snapshot(settings: Any, thread: str) -> None:
    """`GET /chat/sessions/{id}`, and the tail of both lease endpoints and `/chat/abort`."""
    from felix.session.snapshot import gather_thread_snapshot

    await gather_thread_snapshot(settings=settings, tenant_id=TENANT, thread=thread)


async def _lease_then_snapshot(settings: Any, thread: str) -> None:
    """`POST /chat/sessions/lease`: a lease on an unknown thread, then its snapshot."""
    from felix.session.lease import acquire_lease, lease_status, release_lease
    from felix.session.snapshot import gather_thread_snapshot

    result = await acquire_lease(thread, holder_id="h", mode="exclusive", ttl_seconds=30)
    assert result.get("ok"), result
    await lease_status(thread)
    await gather_thread_snapshot(settings=settings, tenant_id=TENANT, thread=thread)
    await release_lease(thread, holder_id="h", token=result["token"])


async def _history(settings: Any, thread: str) -> None:
    """`GET /chat/history/{id}` and `GET /chat/sessions/{id}/export`."""
    from felix.session.store import get_session_store

    session = get_session_store(settings, tenant_id=TENANT).open(thread)
    await session.head()
    assert await session.get_events() == []


async def _search(settings: Any, thread: str) -> None:
    from felix.session.search import search_sessions

    await search_sessions(settings, TENANT, thread.split(":", 1)[1], limit=5)


READS: dict[str, Callable[[Any, str], Awaitable[None]]] = {
    "get_thread_meta": _get_meta,
    "load_leaf": _load_leaf,
    "snapshot": _snapshot,
    "lease": _lease_then_snapshot,
    "history": _history,
    "search": _search,
}


@parametrized
@pytest.mark.parametrize("read", list(READS))
@pytest.mark.asyncio
async def test_reading_an_unknown_thread_does_not_list_it(store_settings: Any, read: str) -> None:
    thread = _unknown_thread()
    await READS[read](store_settings, thread)
    assert thread not in await _listed(store_settings)


@parametrized
@pytest.mark.asyncio
async def test_writing_thread_meta_lists_the_thread(store_settings: Any) -> None:
    from felix.session.thread_state import update_thread_meta

    thread = _unknown_thread()
    await update_thread_meta(settings=store_settings, tenant_id=TENANT, thread_id=thread, phase="aborted")
    assert thread in await _listed(store_settings)
