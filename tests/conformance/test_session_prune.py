"""Finding and deleting whole threads by id prefix, the same on both backends.

`felix/session/prune.py` is what keeps a heartbeat's firing threads to `keep_threads`. Its twin
walks dicts and its Postgres arm runs `LIKE` and `DELETE`, so the contract is the shape both must
agree on: a prefix is a literal (a `_` or `%` in it widens nothing), a thread is found by an event
or by a metadata row alone, a deleted thread is gone whole -- events and row -- and a thread outside
the list is untouched.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from felix.session.types import AppendableEvent

BACKENDS = ["memory", "postgres"]
TENANT = "conformance"

parametrized = pytest.mark.parametrize("store_settings", BACKENDS, indirect=True)


async def _write(settings: Any, thread_id: str) -> None:
    from felix.session.store import get_session_store

    await (
        get_session_store(settings, tenant_id=TENANT)
        .open(thread_id)
        .append(AppendableEvent(kind="message", role="user", content="hello"))
    )


@parametrized
async def test_threads_under_a_prefix_are_found_and_deleted_whole(store_settings: Any) -> None:
    from felix.session.prune import delete_threads, threads_under
    from felix.session.store import get_session_store
    from felix.session.thread_state import claim_thread, thread_exists

    run = uuid.uuid4().hex[:8]
    prefix = f"{TENANT}:job:hb-a_b%{run}:"
    firing_a, firing_b = f"{prefix}one", f"{prefix}two"
    meta_only = f"{prefix}three"
    # What `_` and `%` would reach as `LIKE` wildcards, and a thread beside the prefix.
    lookalike = f"{TENANT}:job:hb-aXbY{run}:four"
    conversation = f"{TENANT}:hb-{run}"
    for thread in (firing_a, firing_b, lookalike, conversation):
        await _write(store_settings, thread)
    assert await claim_thread(settings=store_settings, tenant_id=TENANT, thread_id=meta_only)
    assert await claim_thread(settings=store_settings, tenant_id=TENANT, thread_id=firing_a)

    found = await threads_under(store_settings, tenant_id=TENANT, prefix=prefix)
    assert set(found) == {firing_a, firing_b, meta_only}
    assert all(ts > 0 for ts in found.values())

    assert await delete_threads(store_settings, tenant_id=TENANT, thread_ids=[firing_a, meta_only]) == 2
    assert set(await threads_under(store_settings, tenant_id=TENANT, prefix=prefix)) == {firing_b}
    store = get_session_store(store_settings, tenant_id=TENANT)
    for gone in (firing_a, meta_only):
        assert not await thread_exists(store.open(gone), settings=store_settings, tenant_id=TENANT)
    for kept in (firing_b, lookalike, conversation):
        assert await thread_exists(store.open(kept), settings=store_settings, tenant_id=TENANT)
        assert len(await store.open(kept).get_events()) == 1


@parametrized
async def test_a_thread_written_later_reads_as_more_recent(store_settings: Any) -> None:
    """What keeps the newest firing threads: the activity order is the write order on both arms."""
    from felix.session.prune import threads_under

    run = uuid.uuid4().hex[:8]
    prefix = f"{TENANT}:job:hb-order{run}:"
    await _write(store_settings, f"{prefix}older")
    await asyncio.sleep(0.02)
    await _write(store_settings, f"{prefix}newer")
    found = await threads_under(store_settings, tenant_id=TENANT, prefix=prefix)
    assert found[f"{prefix}newer"] > found[f"{prefix}older"]
