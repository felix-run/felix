"""`InMemorySessionStore.open` registers a thread on its first write, not on its first read.

Every read of a thread id goes through `open` -- snapshots, both lease endpoints, history,
export -- and `open` used to file an empty session under any id it was handed, for the life
of the process. Postgres only gains a row when something is inserted; the twin now matches.
"""

from __future__ import annotations

import gc

from felix.session.store import InMemorySessionStore
from felix.session.types import AppendableEvent


def _msg(text: str) -> AppendableEvent:
    return AppendableEvent(kind="message", role="user", content=text)


async def _read_unknown(store: InMemorySessionStore, thread_id: str) -> None:
    session = store.open(thread_id)
    assert await session.get_events() == []
    assert await session.head() == {"seq": 0}
    await session.wake()


async def test_reading_unknown_ids_leaves_the_store_unchanged() -> None:
    store = InMemorySessionStore(tenant_id="acme")
    for i in range(50):
        await _read_unknown(store, f"probe-{i}")
    gc.collect()

    assert len(store._sessions) == 0
    assert len(store._pending) == 0


async def test_open_then_append_registers_the_session() -> None:
    store = InMemorySessionStore(tenant_id="acme")
    session = store.open("t1")
    assert "t1" not in store._sessions

    await session.append(_msg("hello"))

    assert store._sessions["t1"] is session
    # The reference held across the first append is the one the store now hands out.
    reopened = store.open("t1")
    assert reopened is session
    assert [e.content for e in await reopened.get_events()] == ["hello"]


async def test_two_opens_of_one_unknown_id_share_one_log() -> None:
    store = InMemorySessionStore(tenant_id="acme")
    first = store.open("t1")
    second = store.open("t1")

    await first.append(_msg("from first"))
    await second.append(_msg("from second"))

    expected = ["from first", "from second"]
    assert [e.content for e in await first.get_events()] == expected
    assert [e.content for e in await second.get_events()] == expected
    assert [e.seq for e in await store.open("t1").get_events()] == [0, 1]


async def test_reset_of_an_unknown_id_creates_nothing() -> None:
    store = InMemorySessionStore(tenant_id="acme")
    await store.open("never-written").reset()
    gc.collect()

    assert len(store._sessions) == 0
    assert len(store._pending) == 0


async def test_an_empty_batch_registers_nothing() -> None:
    store = InMemorySessionStore(tenant_id="acme")
    session = store.open("t1")
    assert await session.append_batch([]) == []
    assert "t1" not in store._sessions
