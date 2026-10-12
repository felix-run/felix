"""File checkpoints' edges (`felix.tools.workspace_checkpoints`), on the local backend and memory://.

The round trip, the ledger's contract on both stores and the HTTP surface are pinned elsewhere
(`tests/conformance/test_file_checkpoints.py`, `tests/e2e/test_file_checkpoints.py`). These are the
cases those leave out: bookkeeping that fails, a write that fails, a file too large to keep, the
order of changes across anchors, and when checkpointing turns itself off.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from felix.config import Settings
from felix.session import tree
from felix.storage import get_object_store
from felix.tools import checkpoint_ledger as ledger
from felix.tools import workspace_checkpoints as cp
from felix.tools.workspace import EditRefused
from felix.tools.workspace_backend import WorkspaceScope, get_workspace_backend

TENANT = "acme"
THREAD = "acme:t1"
EMPTY = cp.empty_report()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return Settings(workspace_root=str(ws), object_store="memory")


def _scope() -> WorkspaceScope:
    return WorkspaceScope(TENANT, THREAD, "thread")


def _rec(settings: Settings, anchor: str = "a1") -> cp.CheckpointingBackend:
    return cp.CheckpointingBackend(
        get_workspace_backend(settings), settings=settings, tenant_id=TENANT, thread_id=THREAD, anchor=anchor
    )


async def _text(settings: Settings, path: str) -> str | None:
    state, _, data = await cp.file_state(get_workspace_backend(settings), _scope(), path)
    return data.decode() if state == "present" and data is not None else None


async def _seed(settings: Settings, path: str, text: str) -> None:
    await get_workspace_backend(settings).write_file(_scope(), path, text.encode(), False)


async def test_a_change_that_could_not_be_kept_still_happens_and_is_reported_skipped(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed(settings, "a.txt", "original")
    store = get_object_store(settings)
    counted: list[dict[str, Any]] = []
    monkeypatch.setattr(cp, "record_counter", lambda name, labels: counted.append(labels))

    async def refuse(*args: Any, **kwargs: Any) -> None:
        raise OSError("object store down")

    monkeypatch.setattr(store, "put", refuse)
    await _rec(settings).write_file(_scope(), "a.txt", b"changed", False)
    assert await _text(settings, "a.txt") == "changed", "the write went ahead"
    assert counted == [{"stage": "keep"}]
    monkeypatch.undo()
    (row,) = await ledger.captures(settings, TENANT, THREAD, {"a1"})
    assert (row["before_state"], row["after_state"]) == ("unkept", "present"), "the ledger says why"
    assert await cp.restore_files(settings, TENANT, THREAD, {"a1"}) == {**EMPTY, "skipped": ["a.txt"]}
    assert await _text(settings, "a.txt") == "changed"


async def test_a_ledger_that_cannot_be_written_never_blocks_the_write(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def down(*args: Any) -> None:
        raise ConnectionError("database down")

    monkeypatch.setattr(ledger, "_insert", down)
    await _rec(settings).write_file(_scope(), "a.txt", b"written", False)
    assert await _text(settings, "a.txt") == "written"
    assert await ledger.captures(settings, TENANT, THREAD, {"a1"}) == []


async def test_a_write_that_fails_leaves_no_capture_and_no_content(settings: Settings) -> None:
    await _seed(settings, "a.txt", "original")
    with pytest.raises(EditRefused):
        await _rec(settings).edit_file(_scope(), "a.txt", "not there", "x", False)
    assert await ledger.captures(settings, TENANT, THREAD, {"a1"}) == []
    key = ledger.checkpoint_key(TENANT, ledger._sha(b"original"))
    assert await get_object_store(settings).get(key) is None, "the content it kept went with it"
    assert await ledger.kept_bytes(settings, TENANT) == 0


async def test_a_file_too_large_to_keep_is_skipped_and_one_grown_past_it_is_a_conflict(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cp, "MAX_CHECKPOINT_BYTES", 10)
    await _seed(settings, "big.txt", "x" * 20)
    await _rec(settings).write_file(_scope(), "big.txt", b"small", False)
    await _seed(settings, "grown.txt", "small")
    await _rec(settings).write_file(_scope(), "grown.txt", b"y" * 20, False)
    out = await cp.restore_files(settings, TENANT, THREAD, {"a1"})
    assert out["skipped"] == ["big.txt"] and out["conflicts"] == ["grown.txt"]
    assert await _text(settings, "big.txt") == "small"


async def test_the_first_change_restores_and_the_last_decides_the_conflict(settings: Settings) -> None:
    await _seed(settings, "a.txt", "v0")
    await _rec(settings, "a1").write_file(_scope(), "a.txt", b"v1", False)
    await _rec(settings, "a1").write_file(_scope(), "a.txt", b"v2", False)
    await _rec(settings, "a2").write_file(_scope(), "a.txt", b"v3", False)
    rows = await ledger.captures(settings, TENANT, THREAD, {"a1", "a2"})
    assert [r["before_state"] for r in rows] == ["stored", "stored", "stored"]
    assert (await cp.restore_files(settings, TENANT, THREAD, {"a2"}))["restored"] == ["a.txt"]
    assert await _text(settings, "a.txt") == "v2", "undoing only the later anchor"
    await _seed(settings, "a.txt", "v3")
    assert (await cp.restore_files(settings, TENANT, THREAD, {"a1", "a2"}))["restored"] == ["a.txt"]
    assert await _text(settings, "a.txt") == "v0", "undoing both goes back to before the first"


async def test_a_change_whose_outcome_was_not_recorded_is_a_conflict(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed(settings, "a.txt", "v0")

    async def lost(*args: Any) -> None:
        raise ConnectionError("database down")

    monkeypatch.setattr(ledger, "_update", lost)
    await _rec(settings).write_file(_scope(), "a.txt", b"v1", False)
    monkeypatch.undo()
    assert (await cp.restore_files(settings, TENANT, THREAD, {"a1"}))["conflicts"] == ["a.txt"]
    assert await _text(settings, "a.txt") == "v1"


async def test_a_file_already_as_it_was_is_left_alone(settings: Settings) -> None:
    await _seed(settings, "a.txt", "v0")
    await _rec(settings).write_file(_scope(), "a.txt", b"v0", False)
    assert await cp.restore_files(settings, TENANT, THREAD, {"a1"}) == EMPTY


def test_checkpointing_wraps_only_a_threads_run_with_a_leaf(settings: Settings) -> None:
    backend = get_workspace_backend(settings)
    tree.set_leaf(THREAD, "a1")
    try:
        assert isinstance(cp.checkpointing(backend, settings, _scope()), cp.CheckpointingBackend)
        off = settings.model_copy(update={"file_checkpoints": False})
        assert cp.checkpointing(backend, off, _scope()) is backend
        assert cp.checkpointing(backend, settings, None) is backend
        assert cp.checkpointing(backend, settings, WorkspaceScope(TENANT, None, "thread")) is backend
        assert cp.checkpointing(backend, settings, WorkspaceScope("", THREAD, "thread")) is backend
        for shared in ("tenant", "deployment"):
            assert cp.checkpointing(backend, settings, WorkspaceScope(TENANT, THREAD, shared)) is backend, (
                shared
            )
    finally:
        tree.set_leaf(THREAD, None)
    assert cp.checkpointing(backend, settings, _scope()) is backend, "no leaf to anchor to"


async def test_the_sdk_asks_for_files_back_only_when_told_to(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    import httpx
    from felix_client import FelixClient

    from tests.support.http_bind import bind_transport

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    monkeypatch.setattr(httpx, "AsyncClient", bind_transport(httpx.MockTransport(handler)))
    client = FelixClient(base_url="http://felix")
    await client.rewind("e1", thread_id="t1")
    await client.rewind("e1", thread_id="t1", restore_files=True)
    plain, restoring = (json.loads(r.content) for r in seen)
    assert plain == {"thread_id": "t1", "event_id": "e1"}
    assert restoring == {"thread_id": "t1", "event_id": "e1", "restore_files": True}


async def test_a_file_an_edit_left_too_large_to_compare_is_a_conflict(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Read back after an append, a file past the cap has no digest: nothing can say it is still
    what the change left, so it is not overwritten."""
    monkeypatch.setattr(cp, "MAX_CHECKPOINT_BYTES", 10)
    await _seed(settings, "log.txt", "short")
    await _rec(settings).write_file(_scope(), "log.txt", b" and then much longer", True)
    (row,) = await ledger.captures(settings, TENANT, THREAD, {"a1"})
    assert row["after_state"] == "too_large"
    assert (await cp.restore_files(settings, TENANT, THREAD, {"a1"}))["conflicts"] == ["log.txt"]


async def test_kept_content_that_does_not_match_its_digest_is_not_restored(settings: Settings) -> None:
    await _seed(settings, "a.txt", "original")
    await _rec(settings).write_file(_scope(), "a.txt", b"changed", False)
    (row,) = await ledger.captures(settings, TENANT, THREAD, {"a1"})
    await get_object_store(settings).put(ledger.checkpoint_key(TENANT, row["before_sha"]), b"tampered")
    assert (await cp.restore_files(settings, TENANT, THREAD, {"a1"}))["skipped"] == ["a.txt"]
    assert await _text(settings, "a.txt") == "changed"


def test_every_backend_member_is_spelled_out_on_the_recording_backend() -> None:
    """No member reaches the inner backend by default: a write the Protocol grows, or a folder
    operation a tool starts calling, must be decided here rather than pass through unrecorded."""
    from felix.tools.workspace_backend import WorkspaceBackend

    members = {n for n in vars(WorkspaceBackend) if not n.startswith("_")}
    assert members, "the Protocol lists no members to check"
    missing = sorted(n for n in members if n not in vars(cp.CheckpointingBackend))
    assert missing == []
    assert not hasattr(cp.CheckpointingBackend, "__getattr__")


async def test_folder_operations_are_refused_rather_than_passed_through(settings: Settings) -> None:
    rec = _rec(settings)
    with pytest.raises(RuntimeError, match="not checkpointed"):
        await rec.delete_dir(_scope(), "dir")
    with pytest.raises(RuntimeError, match="not checkpointed"):
        await rec.rename_dir(_scope(), "dir", "other")


async def test_past_the_tenants_ceiling_a_change_is_recorded_but_not_kept(settings: Settings) -> None:
    capped = settings.model_copy(update={"file_checkpoint_max_bytes_per_tenant": 10})
    await _seed(capped, "small.txt", "12345")
    await _seed(capped, "big.txt", "1234567890")
    await _rec(capped).write_file(_scope(), "small.txt", b"x", False)
    await _rec(capped).write_file(_scope(), "big.txt", b"y", False)
    rows = await ledger.captures(capped, TENANT, THREAD, {"a1"})
    assert [(r["path"], r["before_state"]) for r in rows] == [("small.txt", "stored"), ("big.txt", "unkept")]
    assert await ledger.kept_bytes(capped, TENANT) == 5
    out = await cp.restore_files(capped, TENANT, THREAD, {"a1"})
    assert out["restored"] == ["small.txt"] and out["skipped"] == ["big.txt"]


async def test_a_restore_past_its_cap_says_it_is_truncated(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cp, "MAX_RESTORE_CAPTURES", 2)
    for name in ("a", "b", "c"):
        await _rec(settings).write_file(_scope(), name, b"new", False)
    out = await cp.restore_files(settings, TENANT, THREAD, {"a1"})
    assert out["truncated"] and out["removed"] == ["a", "b"]
    assert await _text(settings, "c") == "new"


async def test_a_restore_never_writes_a_reserved_path_whatever_the_ledger_says(settings: Settings) -> None:
    """No tool records `.git/...` -- each refuses it first -- so a row naming one came from somewhere
    else. The restore refuses it too rather than trusting the row."""
    await _seed(settings, "ok.txt", "ok")
    await _rec(settings).write_file(_scope(), "ok.txt", b"changed", False)
    (template,) = await ledger.captures(settings, TENANT, THREAD, {"a1"})
    await ledger._insert(
        settings,
        {
            **template,
            "capture_id": "0" * 20 + "ffffffff",
            "path": ".git/config",
            "before_state": "absent",
            "after_state": "absent",
            "after_sha": None,
        },
    )
    out = await cp.restore_files(settings, TENANT, THREAD, {"a1"})
    assert out["skipped"] == [".git/config"] and out["restored"] == ["ok.txt"]


async def test_content_another_capture_still_names_still_counts_against_the_ceiling(
    settings: Settings,
) -> None:
    """The bypass a review found: forgetting the capture that first kept some content dropped its
    bytes from the sum while another capture kept the object alive, so repeating it admitted
    unlimited content under a ceiling that read zero."""
    capped = settings.model_copy(update={"file_checkpoint_max_bytes_per_tenant": 15})
    other = "acme:t2"
    other_scope = WorkspaceScope(TENANT, other, "thread")
    plain = get_workspace_backend(capped)
    for scope in (_scope(), other_scope):
        await plain.write_file(scope, "f", b"0123456789", False)
    await _rec(capped).write_file(_scope(), "f", b"a", False)
    await cp.CheckpointingBackend(
        plain, settings=capped, tenant_id=TENANT, thread_id=other, anchor="b1"
    ).write_file(other_scope, "f", b"b", False)
    await ledger.forget_anchored(capped, get_object_store(capped), TENANT, THREAD, {"a1"})
    assert await ledger.kept_bytes(capped, TENANT) == 10
    await _seed(capped, "g", "abcdefghij")
    await _rec(capped, "a2").write_file(_scope(), "g", b"x", False)
    (row,) = await ledger.captures(capped, TENANT, THREAD, {"a2"})
    assert row["before_state"] == "unkept", "10 kept + 10 more is past 15"


async def test_content_that_does_not_match_its_digest_is_dropped_so_it_can_be_kept_again(
    settings: Settings,
) -> None:
    await _seed(settings, "a.txt", "original")
    await _rec(settings).write_file(_scope(), "a.txt", b"changed", False)
    key = ledger.checkpoint_key(TENANT, ledger._sha(b"original"))
    store = get_object_store(settings)
    await store.put(key, b"cut sho")
    assert (await cp.restore_files(settings, TENANT, THREAD, {"a1"}))["skipped"] == ["a.txt"]
    assert await store.get(key) is None


async def test_a_rewind_that_cannot_read_its_branch_still_answers(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The leaf is already stored when the branch is read for its files: a store error there
    answers `files: {"error": "unavailable"}`, never a 500 for a rewind that happened."""
    from felix.session import branch
    from felix.session.store import get_session_store
    from felix.session.types import AppendableEvent

    session = get_session_store(settings, tenant_id=TENANT).open(THREAD)
    ids = await tree.annotate_and_append(
        session, [AppendableEvent(kind="message", role="user", content=t) for t in ("one", "two")]
    )
    real_rewind = branch.rewind_to

    async def rewind_then_break(s: Any, target: str) -> Any:
        out = await real_rewind(s, target)

        async def broken() -> Any:
            raise ConnectionError("session store down")

        monkeypatch.setattr(s, "get_events", broken)
        return out

    monkeypatch.setattr(branch, "rewind_to", rewind_then_break)
    out = await branch.rewind_and_persist(
        session, ids[0], settings=settings, tenant_id=TENANT, summarize=False, restore_files=True
    )
    assert out["ok"] and out["files"] == {"error": "unavailable"}
