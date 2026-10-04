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


# --- Postgres is the source of truth, not a per-process cache ------------------------------
#
# Each process keeps `thread_state._meta_by_thread` and the tree module's leaf index. On
# Postgres those were read as the truth, so a second replica -- whose caches are empty, or hold
# what it saw last -- wrote defaults over fields it was not changing and kept serving what it
# had cached after another replica changed it. `_other_replica()` empties both, which is the
# state a replica that has not served the thread (or has not served it since) is in.


def _other_replica() -> None:
    from felix.session import tree
    from felix.session.thread_state import reset_thread_meta_for_tests

    reset_thread_meta_for_tests()
    tree._leaf_by_thread.clear()
    tree._label_by_event.clear()


async def _stored(settings: Any, thread: str) -> dict[str, Any]:
    """The row's own `labels_json` -- what is stored, not what a read merges defaults into."""
    from felix.db.models import ThreadState
    from felix.db.session import tenant_session

    async with tenant_session(settings, TENANT) as db:
        row = await db.get(ThreadState, (TENANT, thread))
        assert row is not None
        return dict(row.labels_json)


@parametrized
@pytest.mark.asyncio
async def test_a_cold_replica_write_keeps_the_fields_it_did_not_change(store_settings: Any) -> None:
    from felix.session.thread_state import get_thread_meta, update_thread_meta

    thread = _unknown_thread()
    a = await update_thread_meta(
        settings=store_settings,
        tenant_id=TENANT,
        thread_id=thread,
        session_name="Alpha",
        phase="running",
        labels={"ev1": "checkpoint"},
    )
    # On `memory://` the process *is* the store, so there is no second replica to be; the
    # memory arm still pins the merge itself.
    if not store_settings.database_url.startswith("memory://"):
        _other_replica()
    b = await update_thread_meta(
        settings=store_settings, tenant_id=TENANT, thread_id=thread, thinking_level="high"
    )

    meta = await get_thread_meta(settings=store_settings, tenant_id=TENANT, thread_id=thread)
    for got in (b, meta):
        assert got["session_name"] == "Alpha"
        assert got["phase"] == "running"
        assert got["labels"] == {"ev1": "checkpoint"}
        assert got["thinking_level"] == "high"
        assert got["created_at"] == a["created_at"]
        assert got["revision"] == a["revision"] + 1


@pytest.mark.parametrize("store_settings", ["postgres"], indirect=True)
@pytest.mark.asyncio
async def test_the_row_keeps_what_a_cold_replica_did_not_write(store_settings: Any) -> None:
    """The stored row, not just the read: a cold write once stored defaults over the name."""
    from felix.session.thread_state import update_thread_meta

    thread = _unknown_thread()
    await update_thread_meta(
        settings=store_settings, tenant_id=TENANT, thread_id=thread, session_name="Alpha", phase="running"
    )
    _other_replica()
    await update_thread_meta(
        settings=store_settings, tenant_id=TENANT, thread_id=thread, thinking_level="high"
    )
    stored = await _stored(store_settings, thread)
    assert (stored["session_name"], stored["phase"], stored["thinking_level"]) == ("Alpha", "running", "high")
    assert stored["revision"] == 2


@pytest.mark.parametrize("store_settings", ["postgres"], indirect=True)
@pytest.mark.asyncio
async def test_a_replica_reads_another_replicas_rename(store_settings: Any) -> None:
    """A warm cache is no excuse: B has read the thread, A renames it, B reads the rename."""
    from felix.session import thread_state
    from felix.session.thread_state import get_thread_meta, update_thread_meta

    thread = _unknown_thread()
    await update_thread_meta(settings=store_settings, tenant_id=TENANT, thread_id=thread, session_name="One")
    _other_replica()
    b_first = await get_thread_meta(settings=store_settings, tenant_id=TENANT, thread_id=thread)
    assert b_first["session_name"] == "One"
    # B's process now holds what it read; A's rename lands in the row behind it.
    b_cache = {k: dict(v) for k, v in thread_state._meta_by_thread.items()}
    await update_thread_meta(settings=store_settings, tenant_id=TENANT, thread_id=thread, session_name="Two")
    thread_state._meta_by_thread.clear()
    thread_state._meta_by_thread.update(b_cache)

    b_second = await get_thread_meta(settings=store_settings, tenant_id=TENANT, thread_id=thread)
    assert b_second["session_name"] == "Two"
    assert b_second["revision"] == 2


@pytest.mark.parametrize("store_settings", ["postgres"], indirect=True)
@pytest.mark.asyncio
async def test_a_replica_reads_the_leaf_another_replica_rewound_to(store_settings: Any) -> None:
    from felix.session import tree
    from felix.session.thread_state import load_leaf, persist_leaf, update_thread_meta

    thread = _unknown_thread()
    await update_thread_meta(settings=store_settings, tenant_id=TENANT, thread_id=thread, session_name="n")
    await persist_leaf(settings=store_settings, tenant_id=TENANT, thread_id=thread, leaf_event_id="a-leaf")
    a_leaves = dict(tree._leaf_by_thread)
    _other_replica()
    await persist_leaf(settings=store_settings, tenant_id=TENANT, thread_id=thread, leaf_event_id="b-leaf")
    # Back on A, whose process still holds the leaf it set.
    tree._leaf_by_thread.clear()
    tree._leaf_by_thread.update(a_leaves)

    assert await load_leaf(settings=store_settings, tenant_id=TENANT, thread_id=thread) == "b-leaf"
    stored = await _stored(store_settings, thread)
    assert stored["session_name"] == "n"
    assert stored["revision"] == 3


@pytest.mark.parametrize("store_settings", ["postgres"], indirect=True)
@pytest.mark.asyncio
async def test_an_append_after_a_rewind_moves_the_stored_leaf(store_settings: Any) -> None:
    """The stored leaf follows a turn's appends, or a read answers the rewind target forever.

    `load_leaf` reads the row on Postgres; before the row was authoritative it read this
    process's index first, which `annotate_and_append` moved and the row did not.
    """
    from felix.session.store import get_session_store
    from felix.session.thread_state import load_leaf, persist_leaf, update_thread_meta
    from felix.session.tree import annotate_and_append
    from felix.session.types import AppendableEvent

    thread = _unknown_thread()
    session = get_session_store(store_settings, tenant_id=TENANT).open(thread)
    await update_thread_meta(settings=store_settings, tenant_id=TENANT, thread_id=thread, phase="idle")
    first = await annotate_and_append(session, [AppendableEvent(kind="message", role="user", content="hi")])
    await persist_leaf(settings=store_settings, tenant_id=TENANT, thread_id=thread, leaf_event_id=first[0])
    later = await annotate_and_append(
        session, [AppendableEvent(kind="message", role="assistant", content="after the rewind")]
    )
    _other_replica()
    assert await load_leaf(settings=store_settings, tenant_id=TENANT, thread_id=thread) == later[-1]


@pytest.mark.parametrize("store_settings", ["postgres"], indirect=True)
@pytest.mark.asyncio
async def test_concurrent_writes_to_different_fields_all_land(store_settings: Any) -> None:
    import asyncio

    from felix.session.thread_state import get_thread_meta, update_thread_meta

    thread = _unknown_thread()
    await update_thread_meta(settings=store_settings, tenant_id=TENANT, thread_id=thread, phase="idle")
    writes: list[dict[str, Any]] = [
        {"session_name": "named"},
        {"phase": "running"},
        {"thinking_level": "high"},
        {"model_id": "m-1"},
        {"labels": {"e1": "one"}},
        {"labels": {"e2": "two"}},
        {"feedback": {"e1": {"rating": 1}}},
        {"parent_session_id": f"{TENANT}:parent"},
    ]
    await asyncio.gather(
        *(
            update_thread_meta(settings=store_settings, tenant_id=TENANT, thread_id=thread, **w)
            for w in writes
        )
    )
    meta = await get_thread_meta(settings=store_settings, tenant_id=TENANT, thread_id=thread)
    assert meta["session_name"] == "named"
    assert meta["phase"] == "running"
    assert meta["thinking_level"] == "high"
    assert meta["model_id"] == "m-1"
    assert meta["labels"] == {"e1": "one", "e2": "two"}
    assert meta["feedback"] == {"e1": {"rating": 1}}
    assert meta["parent_session_id"] == f"{TENANT}:parent"
    assert meta["revision"] == 1 + len(writes)


@pytest.mark.parametrize("store_settings", ["postgres"], indirect=True)
@pytest.mark.asyncio
async def test_two_replicas_creating_one_thread_make_one_row_with_both_writes(store_settings: Any) -> None:
    import asyncio

    from felix.db.models import ThreadState
    from felix.db.session import tenant_session
    from felix.session.thread_state import get_thread_meta, update_thread_meta
    from sqlalchemy import func, select

    thread = _unknown_thread()
    await asyncio.gather(
        update_thread_meta(settings=store_settings, tenant_id=TENANT, thread_id=thread, session_name="first"),
        update_thread_meta(settings=store_settings, tenant_id=TENANT, thread_id=thread, phase="running"),
    )
    async with tenant_session(store_settings, TENANT) as db:
        rows = await db.scalar(
            select(func.count()).select_from(ThreadState).where(ThreadState.thread_id == thread)
        )
    assert rows == 1
    meta = await get_thread_meta(settings=store_settings, tenant_id=TENANT, thread_id=thread)
    assert (meta["session_name"], meta["phase"], meta["revision"]) == ("first", "running", 2)


@pytest.mark.parametrize("store_settings", ["postgres"], indirect=True)
@pytest.mark.asyncio
async def test_a_key_added_to_the_defaults_later_still_answers(store_settings: Any) -> None:
    """A row written before a default existed reads that default rather than missing it."""
    from felix.db.models import ThreadState
    from felix.db.session import tenant_session
    from felix.session.thread_state import get_thread_meta

    thread = _unknown_thread()
    async with tenant_session(store_settings, TENANT) as db:
        db.add(
            ThreadState(tenant_id=TENANT, thread_id=thread, labels_json={"session_name": "old"}, updated_at=1)
        )
        await db.commit()
    meta = await get_thread_meta(settings=store_settings, tenant_id=TENANT, thread_id=thread)
    assert meta["session_name"] == "old"
    assert meta["thinking_level"] == "off"
    assert meta["labels"] == {}
