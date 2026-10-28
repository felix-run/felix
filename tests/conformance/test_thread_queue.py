"""A thread's queue: `thread_state.push_thread_queue` and `take_thread_queue`, on both backends.

What a scheduled task reports while a run is going on in the conversation waits here
(`session/idle_append.py:append_or_queue`): pushed by the worker, taken by whichever process ends
the run. The take must hand an item out once and the push must keep only the newest, or a report
lands twice or the row grows without bound -- and the in-memory twin saying so proves nothing about
the row lock Postgres takes.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

BACKENDS = ["memory", "postgres"]
TENANT = "conformance"
KEY = "pending_entries"

parametrized = pytest.mark.parametrize("store_settings", BACKENDS, indirect=True)


def _thread() -> str:
    return f"{TENANT}:{uuid.uuid4().hex}"


async def _push(settings: Any, thread: str, n: int, *, keep: int = 5) -> None:
    from felix.session.thread_state import push_thread_queue

    await push_thread_queue(
        settings=settings, tenant_id=TENANT, thread_id=thread, name=KEY, items=[{"n": n}], keep=keep
    )


async def _take(settings: Any, thread: str) -> list[dict[str, Any]]:
    from felix.session.thread_state import take_thread_queue

    return await take_thread_queue(settings=settings, tenant_id=TENANT, thread_id=thread, name=KEY)


@parametrized
async def test_a_take_hands_out_what_was_pushed_once_in_order(store_settings: Any) -> None:
    thread = _thread()
    for n in range(3):
        await _push(store_settings, thread, n)
    assert await _take(store_settings, thread) == [{"n": 0}, {"n": 1}, {"n": 2}]
    assert await _take(store_settings, thread) == []


@parametrized
async def test_a_push_keeps_only_the_newest(store_settings: Any) -> None:
    thread = _thread()
    for n in range(7):
        await _push(store_settings, thread, n, keep=5)
    assert [i["n"] for i in await _take(store_settings, thread)] == [2, 3, 4, 5, 6]


@parametrized
async def test_the_queue_leaves_the_threads_other_metadata_alone(store_settings: Any) -> None:
    from felix.session.thread_state import QUEUES_KEY, get_thread_meta, update_thread_meta

    thread = _thread()
    await update_thread_meta(settings=store_settings, tenant_id=TENANT, thread_id=thread, session_name="kept")
    await _push(store_settings, thread, 1)
    # While it waits: no read of the thread's metadata shows the queue -- not a read, and not the
    # metadata a write returns, which `POST /chat/sessions/name` hands its caller.
    read = await get_thread_meta(settings=store_settings, tenant_id=TENANT, thread_id=thread)
    written = await update_thread_meta(
        settings=store_settings, tenant_id=TENANT, thread_id=thread, session_name="renamed"
    )
    for meta in (read, written):
        assert QUEUES_KEY not in meta and KEY not in meta, meta
    assert await _take(store_settings, thread) == [{"n": 1}], "the write dropped the queue"
    meta = await get_thread_meta(settings=store_settings, tenant_id=TENANT, thread_id=thread)
    assert meta["session_name"] == "renamed"


@parametrized
async def test_a_put_back_goes_ahead_of_what_was_queued_since(store_settings: Any) -> None:
    from felix.session.thread_state import push_thread_queue

    thread = _thread()
    await _push(store_settings, thread, 3)
    await push_thread_queue(
        settings=store_settings,
        tenant_id=TENANT,
        thread_id=thread,
        name=KEY,
        items=[{"n": 1}, {"n": 2}],
        keep=5,
        front=True,
    )
    assert [i["n"] for i in await _take(store_settings, thread)] == [1, 2, 3]


@parametrized
async def test_taking_from_an_unknown_thread_lists_nothing(store_settings: Any) -> None:
    from tests.support.session_listing import every_listed

    thread = _thread()
    assert await _take(store_settings, thread) == []
    assert thread not in {str(m["id"]) for m in await every_listed(store_settings, TENANT)}
