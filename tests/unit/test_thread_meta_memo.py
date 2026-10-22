"""The two races the turn's round-trip savings open, each closed by a count.

`tests/conformance/test_turn_round_trips.py` pins what the memo and the known head save. These pin
what they must not get wrong when a request's own tasks interleave, which no store arm reproduces
on demand: the Postgres row read is replaced here so the interleaving can be placed exactly.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from felix.session.types import AppendableEvent

from tests.support.factories import make_settings

TENANT = "t"
THREAD = "t:th"


async def test_a_read_that_straddles_a_write_is_not_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    """A read that began during a tool's write lands after it; the next read sees the write."""
    from felix.context import AuthContext, RequestContext, run_with_context
    from felix.session import thread_state
    from felix.session.meta_memo import memoize_thread_meta

    # Not `memory://`: the memo serves the Postgres arm only, whose row read is replaced below.
    settings = make_settings(database_url="postgresql+psycopg://u@127.0.0.1:9/none")
    rows = [SimpleNamespace(labels_json={"thinking_level": "low"})]
    reads: list[str] = []
    out, release = asyncio.Event(), asyncio.Event()

    async def read_row(_settings: Any, _tenant: str, _thread: str) -> Any:
        row = rows[-1]
        reads.append(row.labels_json["thinking_level"])
        if len(reads) == 1:
            out.set()
            await release.wait()
        return row

    monkeypatch.setattr(thread_state, "_read_row", read_row)

    @asynccontextmanager
    async def no_database(_settings: Any, _tenant: str) -> AsyncIterator[None]:
        yield None

    # `_writing` is under test, the session it opens is not.
    monkeypatch.setattr("felix.db.session.tenant_session", no_database)

    async def level() -> str:
        meta = await thread_state.get_thread_meta(settings=settings, tenant_id=TENANT, thread_id=THREAD)
        return meta["thinking_level"]

    with run_with_context(RequestContext(settings=settings, auth=AuthContext(tenant_id=TENANT))):
        memoize_thread_meta()
        # A tool's write, through the context every writer in `thread_state` uses. The read starts
        # inside it -- after the forget on the way in, before the commit -- which only the forget
        # on the way out can drop.
        async with thread_state._writing(settings, TENANT, THREAD):
            slow = asyncio.create_task(level())
            await out.wait()
            rows.append(SimpleNamespace(labels_json={"thinking_level": "high"}))
        release.set()
        assert await slow == "low", "the straddling read answers what it read"
        assert await level() == "high", "and is not what later readers in the request are handed"
        assert await level() == "high"
    assert reads == ["low", "high"], "the third read is the memo's"


async def test_an_append_announced_across_a_reset_records_no_head(monkeypatch: pytest.MonkeyPatch) -> None:
    """An append that wrote before a reset and announced after it would name deleted events.

    Through a real append, with the reset landing after the write and before the announce: on
    the memory twin that is the search-index call between them. So where the append notes the
    reset count is pinned too -- noting it after the write, just before the announce, would see
    the reset and keep the head.
    """
    from felix.session import heads, store

    real = store._index_for_search

    def reset_lands(**kwargs: Any) -> None:
        real(**kwargs)
        heads.note_reset(TENANT, THREAD)

    session = store.get_session_store(make_settings(), tenant_id=TENANT).open(THREAD)
    monkeypatch.setattr(store, "_index_for_search", reset_lands)
    await session.append_batch([AppendableEvent(kind="message", role="user", content="a")])
    assert heads.known_head(TENANT, THREAD) is None

    monkeypatch.setattr(store, "_index_for_search", real)
    await session.append_batch([AppendableEvent(kind="message", role="user", content="b")])
    assert heads.known_head(TENANT, THREAD) == 2
