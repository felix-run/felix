"""`POST /chat/rewind` with `restore_files`: the files the model changed after the rewind point
come back as they were, over HTTP against the real app, with the file tools a scripted model calls.

What is pinned is what a client is promised: every kind of change the file tools make is undone
(a write, an edit, a delete, a rename, a new file), a file changed since by anyone else is reported
and left alone, a rewind without `restore_files` leaves the workspace as it is, and a rewind to a
point after the change keeps it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from felix.manifests.loader import parse_manifest
from felix_ai.providers.scripted import ScriptedTurn
from felix_ai.types import ToolCall

TENANT = "default"
THREAD = "e2e-checkpoints"
TOOLS = ["write_file", "edit_file", "multi_edit", "delete_file", "rename_file", "read_file"]
EMPTY = {"restored": [], "removed": [], "conflicts": [], "skipped": [], "truncated": False}


def _agent() -> Any:
    spec = {"pattern": "react", "auth": {"inbound": {"allow_anonymous": True}}, "tools": TOOLS}
    return parse_manifest(
        {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": "e2e-cp"}, "spec": spec}
    )


def _scope_dir(root: Path) -> Path:
    from felix.config import get_settings
    from felix.tools.workspace_scope import scope_relpath

    rel = scope_relpath(get_settings(), TENANT, f"{TENANT}:{THREAD}", "thread")
    path = root.joinpath(*rel.split("/"))
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def root(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


def _calls(*calls: tuple[str, dict[str, Any]]) -> list[ScriptedTurn]:
    turns = [
        ScriptedTurn(tool_calls=[ToolCall(id=f"c{i}", name=n, args=a)]) for i, (n, a) in enumerate(calls)
    ]
    return [*turns, ScriptedTurn(content="done")]


async def _turn(app: Any, text: str) -> None:
    resp = await app.client.post(
        "/chat",
        json={"manifest": "e2e-cp", "thread_id": THREAD, "messages": [{"role": "user", "content": text}]},
    )
    assert resp.status_code == 200, resp.text


async def _user_event(app: Any, text: str) -> str:
    """The id of the user message `text`, from the session snapshot's transcript."""
    snap = (await app.client.get(f"/chat/sessions/{THREAD}")).json()
    [eid] = [e["id"] for e in snap["transcript"] if e.get("role") == "user" and e.get("content") == text]
    return eid


async def _rewind(app: Any, event_id: str, **body: Any) -> Any:
    resp = await app.client.post(
        "/chat/rewind", json={"thread_id": THREAD, "event_id": event_id, "summarize": False, **body}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_a_rewind_puts_back_every_kind_of_change_the_file_tools_made(boot: Any, root: Path) -> None:
    ws = _scope_dir(root)
    (ws / "kept.txt").write_text("kept\n")
    (ws / "gone.txt").write_text("gone\n")
    (ws / "moved.txt").write_text("moved\n")
    (ws / "multi.txt").write_text("alpha beta\n")
    first = _calls(("write_file", {"path": "a.txt", "content": "one\n"}))
    second = _calls(
        ("edit_file", {"path": "a.txt", "old_string": "one", "new_string": "two"}),
        ("write_file", {"path": "new.txt", "content": "new\n"}),
        ("delete_file", {"path": "gone.txt"}),
        ("rename_file", {"path": "moved.txt", "to_path": "there.txt"}),
        ("multi_edit", {"path": "multi.txt", "edits": [{"old_string": "alpha", "new_string": "A"}]}),
    )
    async with boot(
        [*first, *second], env={"FELIX_WORKSPACE_ROOT": str(root)}, manifests={"e2e-cp": _agent()}
    ) as app:
        await _turn(app, "first")
        await _turn(app, "second")
        assert (ws / "a.txt").read_text() == "two\n" and not (ws / "gone.txt").exists()
        out = await _rewind(app, await _user_event(app, "second"), restore_files=True)
    assert out["files"] == {
        **EMPTY,
        "restored": ["a.txt", "gone.txt", "moved.txt", "multi.txt"],
        "removed": ["new.txt", "there.txt"],
    }
    assert (ws / "multi.txt").read_text() == "alpha beta\n"
    assert (ws / "a.txt").read_text() == "one\n", (
        "the first turn's write stays: it came before the rewind point"
    )
    assert (ws / "gone.txt").read_text() == "gone\n" and (ws / "moved.txt").read_text() == "moved\n"
    assert not (ws / "new.txt").exists() and not (ws / "there.txt").exists()
    assert (ws / "kept.txt").read_text() == "kept\n"


async def test_a_file_changed_since_is_reported_and_left_alone(boot: Any, root: Path) -> None:
    ws = _scope_dir(root)
    script = [
        *_calls(("write_file", {"path": "a.txt", "content": "one\n"})),
        *_calls(
            ("write_file", {"path": "a.txt", "content": "two\n"}),
            ("write_file", {"path": "b.txt", "content": "b\n"}),
        ),
    ]
    async with boot(script, env={"FELIX_WORKSPACE_ROOT": str(root)}, manifests={"e2e-cp": _agent()}) as app:
        await _turn(app, "first")
        await _turn(app, "second")
        (ws / "a.txt").write_text("the person's edit\n")  # outside the file tools
        out = await _rewind(app, await _user_event(app, "second"), restore_files=True)
    assert out["files"]["conflicts"] == ["a.txt"] and out["files"]["removed"] == ["b.txt"]
    assert (ws / "a.txt").read_text() == "the person's edit\n"


async def test_without_restore_files_a_rewind_leaves_the_workspace_alone(boot: Any, root: Path) -> None:
    ws = _scope_dir(root)
    async with boot(
        _calls(("write_file", {"path": "a.txt", "content": "one\n"})),
        env={"FELIX_WORKSPACE_ROOT": str(root)},
        manifests={"e2e-cp": _agent()},
    ) as app:
        await _turn(app, "first")
        out = await _rewind(app, await _user_event(app, "first"))
    assert "files" not in out and (ws / "a.txt").read_text() == "one\n"


async def test_with_checkpoints_off_nothing_is_recorded_or_restored(boot: Any, root: Path) -> None:
    ws = _scope_dir(root)
    async with boot(
        _calls(("write_file", {"path": "a.txt", "content": "one\n"})),
        env={"FELIX_WORKSPACE_ROOT": str(root), "FELIX_FILE_CHECKPOINTS": "false"},
        manifests={"e2e-cp": _agent()},
    ) as app:
        await _turn(app, "first")
        out = await _rewind(app, await _user_event(app, "first"), restore_files=True)
    assert out["files"] == EMPTY
    assert (ws / "a.txt").read_text() == "one\n"


async def test_a_rewind_to_the_tool_call_itself_undoes_what_that_call_did(boot: Any, root: Path) -> None:
    """The change is anchored at the assistant event whose call made it, and a rewind that lands
    there keeps the call but drops its result: the file must go back too."""
    ws = _scope_dir(root)
    async with boot(
        _calls(("write_file", {"path": "a.txt", "content": "one\n"})),
        env={"FELIX_WORKSPACE_ROOT": str(root)},
        manifests={"e2e-cp": _agent()},
    ) as app:
        await _turn(app, "first")
        snap = (await app.client.get(f"/chat/sessions/{THREAD}")).json()
        [call] = [e["id"] for e in snap["transcript"] if e.get("role") == "assistant" and e.get("toolCalls")]
        out = await _rewind(app, call, restore_files=True)
    assert out["files"]["removed"] == ["a.txt"] and not (ws / "a.txt").exists()


async def test_a_second_rewind_to_the_same_point_undoes_only_what_came_after_the_first(
    boot: Any, root: Path
) -> None:
    """The first rewind undid the call's write and forgot it; the person then made the file their
    own. A second rewind there must put back the person's file, not the state before the first."""
    ws = _scope_dir(root)
    script = [
        *_calls(("write_file", {"path": "a.txt", "content": "agent\n"})),
        *_calls(("write_file", {"path": "a.txt", "content": "again\n"})),
    ]
    async with boot(script, env={"FELIX_WORKSPACE_ROOT": str(root)}, manifests={"e2e-cp": _agent()}) as app:
        await _turn(app, "first")
        call = await _tool_call_event(app)
        assert (await _rewind(app, call, restore_files=True))["files"]["removed"] == ["a.txt"]
        (ws / "a.txt").write_text("the person's file\n")
        await _turn(app, "second")
        out = await _rewind(app, call, restore_files=True)
    assert out["files"] == {**EMPTY, "restored": ["a.txt"]}
    assert (ws / "a.txt").read_text() == "the person's file\n"


async def test_a_rewind_without_restore_forgets_the_branch_too(boot: Any, root: Path) -> None:
    """Keeping the files is a choice about that branch: a later restore does not reach back to it."""
    ws = _scope_dir(root)
    script = [
        *_calls(("write_file", {"path": "a.txt", "content": "kept\n"})),
        *_calls(("write_file", {"path": "b.txt", "content": "b\n"})),
    ]
    async with boot(script, env={"FELIX_WORKSPACE_ROOT": str(root)}, manifests={"e2e-cp": _agent()}) as app:
        await _turn(app, "first")
        call = await _tool_call_event(app)
        await _rewind(app, call)
        await _turn(app, "second")
        out = await _rewind(app, call, restore_files=True)
    assert out["files"] == {**EMPTY, "removed": ["b.txt"]}
    assert (ws / "a.txt").read_text() == "kept\n" and not (ws / "b.txt").exists()


async def test_a_restore_is_audited_with_who_undid_what(boot: Any, root: Path) -> None:
    from felix.flush import flush_all

    _scope_dir(root)
    async with boot(
        _calls(("write_file", {"path": "a.txt", "content": "one\n"})),
        env={"FELIX_WORKSPACE_ROOT": str(root)},
        manifests={"e2e-cp": _agent()},
    ) as app:
        await _turn(app, "first")
        event = await _user_event(app, "first")
        await _rewind(app, event, restore_files=True, manifest="e2e-cp")
        await flush_all(app.settings)
        audit = await app.client.get("/audit", params={"event_type": "workspace_restore"})
    assert audit.status_code == 200, audit.text
    [row] = audit.json()["items"]
    assert (row["manifest_id"], row["status"]) == ("e2e-cp", "ok")
    payload = row["payload_json"]
    assert payload["thread_id"] == f"{TENANT}:{THREAD}" and payload["event_id"] == event
    assert (payload["removed"], payload["removed_count"], payload["restored_count"]) == (["a.txt"], 1, 0)


async def _tool_call_event(app: Any) -> str:
    snap = (await app.client.get(f"/chat/sessions/{THREAD}")).json()
    calls = [e["id"] for e in snap["transcript"] if e.get("role") == "assistant" and e.get("toolCalls")]
    return calls[0]
