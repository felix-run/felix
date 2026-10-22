"""The two races the turn's round-trip savings open, each closed by a count.

`tests/conformance/test_turn_round_trips.py` pins what the memo and the known head save. These pin
what they must not get wrong when a request's own tasks interleave, which no store arm reproduces
on demand: the Postgres row read is replaced here so the interleaving can be placed exactly.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from tests.support.factories import make_settings

TENANT = "t"
THREAD = "t:th"


async def test_a_read_that_straddles_a_write_is_not_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tool's write lands while another task's read is out; the next read sees the write."""
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

    async def level() -> str:
        meta = await thread_state.get_thread_meta(settings=settings, tenant_id=TENANT, thread_id=THREAD)
        return meta["thinking_level"]

    with run_with_context(RequestContext(settings=settings, auth=AuthContext(tenant_id=TENANT))):
        memoize_thread_meta()
        slow = asyncio.create_task(level())
        await out.wait()
        # The write commits, then forgets -- what every writer in `thread_state` does last.
        rows.append(SimpleNamespace(labels_json={"thinking_level": "high"}))
        thread_state._forget(TENANT, THREAD)
        release.set()
        assert await slow == "low", "the straddling read answers what it read"
        assert await level() == "high", "and is not what later readers in the request are handed"
        assert await level() == "high"
    assert reads == ["low", "high"], "the third read is the memo's"


async def test_an_append_announced_across_a_reset_records_no_head() -> None:
    """An append that committed before a reset and announced after it would name deleted events."""
    from felix.session import store

    resets_seen = store._resets
    store._forget_head(TENANT, THREAD)  # The reset lands between the append's commit and announce.
    await store._announce(THREAD, tenant_id=TENANT, next_seq=7, resets_seen=resets_seen)
    assert store.known_head(TENANT, THREAD) is None

    await store._announce(THREAD, tenant_id=TENANT, next_seq=1, resets_seen=store._resets)
    assert store.known_head(TENANT, THREAD) == 1
