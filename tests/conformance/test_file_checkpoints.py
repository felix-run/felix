"""The file-checkpoint ledger (`tools/checkpoint_ledger.py`): one contract, memory and Postgres.

The in-memory twin is what every other test runs on; this holds it to the table it stands in for.
Each case drives the ledger the way production does -- captures taken around a real backend's
writes, a restore through the same backend, the sweep's cross-tenant read and per-tenant forget --
so a twin that sorts, filters, shares content or isolates differently from the table fails here.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.storage import get_object_store
from felix.tools import checkpoint_ledger as ledger
from felix.tools import workspace_checkpoints as cp
from felix.tools.workspace_backend import WorkspaceScope, get_workspace_backend

pytestmark = pytest.mark.parametrize("checkpoint_settings", ["memory", "postgres"], indirect=True)

TENANT = "conformance"
OTHER = "conformance-other"
THREAD = f"{TENANT}:t1"
EMPTY = cp.empty_report()


def _scope(tenant: str = TENANT, thread: str = THREAD) -> WorkspaceScope:
    return WorkspaceScope(tenant, thread, "thread")


def _recording(
    settings: Any, anchor: str, tenant: str = TENANT, thread: str = THREAD
) -> cp.CheckpointingBackend:
    return cp.CheckpointingBackend(
        get_workspace_backend(settings), settings=settings, tenant_id=tenant, thread_id=thread, anchor=anchor
    )


async def _text(settings: Any, path: str) -> str | None:
    state, _, data = await cp.file_state(get_workspace_backend(settings), _scope(), path)
    return data.decode() if state == "present" and data is not None else None


async def test_a_round_trip_puts_every_change_back(checkpoint_settings: Any) -> None:
    settings = checkpoint_settings
    plain = get_workspace_backend(settings)
    await plain.write_file(_scope(), "kept.txt", b"kept", False)
    await plain.write_file(_scope(), "gone.txt", b"gone", False)
    rec = _recording(settings, "a1")
    await rec.write_file(_scope(), "new.txt", b"one", False)
    await rec.write_file(_scope(), "new.txt", b"two", False)
    await rec.edit_file(_scope(), "kept.txt", "kept", "edited", False)
    await rec.delete_file(_scope(), "gone.txt")

    out = await cp.restore_files(settings, TENANT, THREAD, {"a1"})

    assert out == {**EMPTY, "restored": ["gone.txt", "kept.txt"], "removed": ["new.txt"]}
    assert await _text(settings, "kept.txt") == "kept"
    assert await _text(settings, "gone.txt") == "gone"
    assert await _text(settings, "new.txt") is None


async def test_captures_are_read_by_anchor_oldest_first_and_at_most_the_limit(
    checkpoint_settings: Any,
) -> None:
    settings = checkpoint_settings
    for anchor, path in (("a1", "x"), ("a2", "y"), ("a1", "z"), ("a3", "w")):
        await _recording(settings, anchor).write_file(_scope(), path, b"data", False)
    rows = await ledger.captures(settings, TENANT, THREAD, {"a1", "a2"})
    assert [(r["anchor_event_id"], r["path"]) for r in rows] == [("a1", "x"), ("a2", "y"), ("a1", "z")]
    assert all(r["after_state"] == "present" and r["before_state"] == "absent" for r in rows)
    limited = await ledger.captures(settings, TENANT, THREAD, {"a1", "a2"}, limit=2)
    assert [(r["anchor_event_id"], r["path"]) for r in limited] == [("a1", "x"), ("a2", "y")]


async def test_one_tenants_captures_are_invisible_to_another(checkpoint_settings: Any) -> None:
    settings = checkpoint_settings
    await _recording(settings, "a1").write_file(_scope(), "x", b"data", False)
    assert await ledger.captures(settings, OTHER, THREAD, {"a1"}) == []
    assert await cp.restore_files(settings, OTHER, THREAD, {"a1"}) == EMPTY


async def test_content_is_kept_once_per_tenant_and_counted_once(checkpoint_settings: Any) -> None:
    """A file appended to step after step keeps each earlier version once, not once per step, and
    identical content under two anchors shares one object."""
    settings = checkpoint_settings
    plain = get_workspace_backend(settings)
    await plain.write_file(_scope(), "log", b"x" * 100, False)
    await plain.write_file(_scope(), "copy", b"x" * 100, False)
    await _recording(settings, "a1").write_file(_scope(), "log", b"y", True)
    await _recording(settings, "a2").write_file(_scope(), "copy", b"z", True)
    rows = await ledger.captures(settings, TENANT, THREAD, {"a1", "a2"})
    assert [r["before_state"] for r in rows] == ["stored", "stored"]
    assert rows[0]["before_sha"] == rows[1]["before_sha"]
    assert await ledger.kept_bytes(settings, TENANT) == 100


async def test_shared_content_outlives_a_forgotten_capture_while_another_names_it(
    checkpoint_settings: Any,
) -> None:
    settings = checkpoint_settings
    store = get_object_store(settings)
    plain = get_workspace_backend(settings)
    other_thread = f"{TENANT}:t2"
    await plain.write_file(_scope(), "f", b"same", False)
    await plain.write_file(_scope(thread=other_thread), "f", b"same", False)
    await _recording(settings, "a1").write_file(_scope(), "f", b"one", False)
    await _recording(settings, "b1", thread=other_thread).write_file(
        _scope(thread=other_thread), "f", b"two", False
    )
    (row,) = await ledger.captures(settings, TENANT, THREAD, {"a1"})
    key = ledger.checkpoint_key(TENANT, row["before_sha"])

    assert await ledger.forget_path(settings, store, TENANT, THREAD, "f", ledger.now_ms() + 1) == 1
    assert await store.get(key) == b"same", "the other thread still names it"
    assert await ledger.kept_bytes(settings, TENANT) == 4, "and it still counts against the ceiling"
    assert await ledger.forget_path(settings, store, TENANT, other_thread, "f", ledger.now_ms() + 1) == 1
    assert await store.get(key) is None
    assert await ledger.kept_bytes(settings, TENANT) == 0


async def test_the_sweep_reads_each_files_whole_history_across_tenants(
    checkpoint_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = checkpoint_settings
    clock = {"ms": 1_000_000}
    monkeypatch.setattr(ledger, "now_ms", lambda: clock["ms"])
    for tenant in (TENANT, OTHER):
        thread = f"{tenant}:t1"
        await _recording(settings, "old", tenant, thread).write_file(_scope(tenant, thread), "f", b"v", False)
    # In a thread that stays busy: `kept` changed again since, so its whole history stays; `left`,
    # which the agent stopped touching, goes on time.
    await _recording(settings, "old").write_file(_scope(), "kept", b"v", False)
    await _recording(settings, "old").write_file(_scope(), "left", b"v", False)
    clock["ms"] += 10_000
    await _recording(settings, "new").write_file(_scope(), "kept", b"w", False)

    expired = await ledger.expired_paths(settings, older_than_ms=clock["ms"])
    assert expired == [(TENANT, THREAD, "f"), (TENANT, THREAD, "left"), (OTHER, f"{OTHER}:t1", "f")]


async def test_a_file_changed_again_mid_sweep_keeps_its_history(
    checkpoint_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = checkpoint_settings
    clock = {"ms": 1_000_000}
    monkeypatch.setattr(ledger, "now_ms", lambda: clock["ms"])
    await _recording(settings, "old").write_file(_scope(), "f", b"v", False)
    cutoff = clock["ms"] + 1
    assert await ledger.expired_paths(settings, older_than_ms=cutoff) == [(TENANT, THREAD, "f")]
    clock["ms"] += 10_000
    await _recording(settings, "new").write_file(_scope(), "f", b"w", False)  # between read and forget
    assert await ledger.forget_path(settings, get_object_store(settings), TENANT, THREAD, "f", cutoff) == 0
    assert len(await ledger.captures(settings, TENANT, THREAD, {"old", "new"})) == 2


async def test_forgetting_an_anchor_leaves_the_rest_of_the_thread(checkpoint_settings: Any) -> None:
    settings = checkpoint_settings
    await _recording(settings, "a1").write_file(_scope(), "x", b"1", False)
    await _recording(settings, "a2").write_file(_scope(), "y", b"2", False)
    assert await ledger.forget_anchored(settings, get_object_store(settings), TENANT, THREAD, {"a1"}) == 1
    assert [r["path"] for r in await ledger.captures(settings, TENANT, THREAD, {"a1", "a2"})] == ["y"]


@pytest.mark.parametrize("days", [1, 0])
async def test_the_retention_sweep_collects_old_threads_and_keeps_new_ones(
    checkpoint_settings: Any, monkeypatch: pytest.MonkeyPatch, days: int
) -> None:
    """Through `run_retention_sweep`, the way the worker runs it; `0` days keeps everything."""
    from felix.jobs import retention

    settings = checkpoint_settings.model_copy(update={"file_checkpoint_retention_days": days})
    store = get_object_store(settings)
    clock = {"ms": 10 * retention.DAY_MS}
    monkeypatch.setattr(ledger, "now_ms", lambda: clock["ms"])
    monkeypatch.setattr(retention, "now_ms", lambda: clock["ms"])
    plain = get_workspace_backend(settings)
    new_thread = f"{TENANT}:t2"
    await plain.write_file(_scope(), "f", b"old content", False)
    await _recording(settings, "old").write_file(_scope(), "f", b"v1", False)
    clock["ms"] += 2 * retention.DAY_MS
    await plain.write_file(_scope(thread=new_thread), "f", b"new content", False)
    await _recording(settings, "new", thread=new_thread).write_file(
        _scope(thread=new_thread), "f", b"v2", False
    )

    counts = await retention.run_retention_sweep(settings)

    old_key = ledger.checkpoint_key(TENANT, ledger._sha(b"old content"))
    new_key = ledger.checkpoint_key(TENANT, ledger._sha(b"new content"))
    if days:
        assert counts["file_checkpoints"] == 1, "one file's history, in the old thread"
        assert await ledger.captures(settings, TENANT, THREAD, {"old"}) == []
        assert await store.get(old_key) is None
    else:
        assert counts["file_checkpoints"] == 0
        assert await store.get(old_key) == b"old content"
    assert await store.get(new_key) == b"new content"
