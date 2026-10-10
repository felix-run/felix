"""`todo_write`: the agent's checklist, through the real stack.

What a person sees is the point of the tool, so these assert on what reaches them -- the
`todo_updated` frame and the thread snapshot -- as well as on what the model is shown.
"""

from __future__ import annotations

import json
from typing import Any

from felix.manifests.loader import parse_manifest
from felix_ai.providers.scripted import ScriptedTurn
from felix_ai.types import ToolCall

THREAD = "e2e-todos"


def _agent(**spec: Any) -> Any:
    base: dict[str, Any] = {
        "pattern": "react",
        "auth": {"inbound": {"allow_anonymous": True}},
        "tools": ["todo_write"],
    }
    base.update(spec)
    return parse_manifest(
        {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": "e2e-todo"}, "spec": base}
    )


def _write(call_id: str, *items: tuple[str, str]) -> ScriptedTurn:
    todos = [{"content": c, "status": s} for c, s in items]
    return ScriptedTurn(tool_calls=[ToolCall(id=call_id, name="todo_write", args={"todos": todos})])


def _frames(body: str) -> list[dict[str, Any]]:
    return [
        json.loads(line[len("data: ") :])
        for line in body.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]


def _text(messages: list[Any]) -> str:
    return "\n".join(str(getattr(m, "content", "")) for m in messages)


async def _stream(app: Any, text: str = "fix the build") -> list[dict[str, Any]]:
    resp = await app.client.post(
        "/chat/stream",
        json={"manifest": "e2e-todo", "thread_id": THREAD, "messages": [{"role": "user", "content": text}]},
    )
    assert resp.status_code == 200, resp.text
    return _frames(resp.text)


async def _snapshot_todos(app: Any) -> list[dict[str, Any]]:
    resp = await app.client.get(f"/chat/sessions/{THREAD}")
    assert resp.status_code == 200, resp.text
    return resp.json()["todos"]


async def test_the_list_reaches_the_stream_the_snapshot_and_the_model(boot: Any) -> None:
    script = [
        _write(
            "t1",
            ("Reproduce the failure", "in_progress"),
            ("Fix it", "pending"),
            ("Run the tests", "pending"),
        ),
        ScriptedTurn(content="On it."),
    ]
    async with boot(script, manifests={"e2e-todo": _agent()}) as app:
        frames = await _stream(app)
        updates = [f["data"]["todos"] for f in frames if f.get("event") == "todo_updated"]
        assert [[(t["content"], t["status"]) for t in u] for u in updates] == [
            [("Reproduce the failure", "in_progress"), ("Fix it", "pending"), ("Run the tests", "pending")]
        ]
        # Survives the stream: a reload draws it from the snapshot.
        stored = await _snapshot_todos(app)
        assert [(t["id"], t["status"]) for t in stored] == [
            ("1", "in_progress"),
            ("2", "pending"),
            ("3", "pending"),
        ]
        shown = _text(app.spy.prompts[1])
        assert "Todo list updated: 0/3 completed." in shown
        assert "In progress: Reproduce the failure" in shown


async def test_each_write_replaces_the_list(boot: Any) -> None:
    script = [
        _write("t1", ("A", "in_progress"), ("B", "pending")),
        _write("t2", ("A", "completed"), ("B", "in_progress")),
        ScriptedTurn(content="halfway"),
    ]
    async with boot(script, manifests={"e2e-todo": _agent()}) as app:
        await _stream(app)
        assert [(t["content"], t["status"]) for t in await _snapshot_todos(app)] == [
            ("A", "completed"),
            ("B", "in_progress"),
        ]
        assert "Todo list updated: 1/2 completed." in _text(app.spy.prompts[2])


async def test_two_items_in_progress_is_refused_and_nothing_is_stored(boot: Any) -> None:
    script = [_write("t1", ("A", "in_progress"), ("B", "in_progress")), ScriptedTurn(content="oops")]
    async with boot(script, manifests={"e2e-todo": _agent()}) as app:
        frames = await _stream(app)
        assert "at most one item may be in_progress" in _text(app.spy.prompts[1])
        assert not any(f.get("event") == "todo_updated" for f in frames)
        assert await _snapshot_todos(app) == []


async def test_it_is_bound_only_where_a_manifest_names_it(boot: Any) -> None:
    async with boot(
        [ScriptedTurn(content="hi")], manifests={"e2e-todo": _agent(tools=["calculator"])}
    ) as app:
        await _stream(app)
        assert "todo_write" not in app.spy.tools[0]


async def test_frame_and_snapshot_carry_the_same_items_whole(boot: Any) -> None:
    """Every field, ids included, on both surfaces -- not just the ones the first test reads."""
    item = {"content": "Run the tests", "status": "in_progress", "active_form": "Running the tests"}
    script = [
        ScriptedTurn(tool_calls=[ToolCall(id="t1", name="todo_write", args={"todos": [item]})]),
        ScriptedTurn(content="ok"),
    ]
    async with boot(script, manifests={"e2e-todo": _agent()}) as app:
        frames = await _stream(app)
        [update] = [f["data"]["todos"] for f in frames if f.get("event") == "todo_updated"]
        assert update == [{"id": "1", **item}]
        assert await _snapshot_todos(app) == update


async def test_an_empty_write_clears_the_list(boot: Any) -> None:
    script = [
        _write("t1", ("A", "in_progress"), ("B", "pending"), ("C", "pending")),
        _write("t2"),
        ScriptedTurn(content="nothing left"),
    ]
    async with boot(script, manifests={"e2e-todo": _agent()}) as app:
        frames = await _stream(app)
        updates = [f["data"]["todos"] for f in frames if f.get("event") == "todo_updated"]
        assert [len(u) for u in updates] == [3, 0]
        assert await _snapshot_todos(app) == []
        assert "Todo list cleared." in _text(app.spy.prompts[2])


async def test_a_refused_write_leaves_the_last_good_list(boot: Any) -> None:
    script = [
        _write("t1", ("A", "in_progress"), ("B", "pending")),
        _write("t2", ("A", "in_progress"), ("B", "in_progress")),
        ScriptedTurn(content="kept"),
    ]
    async with boot(script, manifests={"e2e-todo": _agent()}) as app:
        await _stream(app)
        assert [(t["content"], t["status"]) for t in await _snapshot_todos(app)] == [
            ("A", "in_progress"),
            ("B", "pending"),
        ]


async def test_a_rewind_shows_the_list_that_branch_had(boot: Any) -> None:
    """The list is read off the current branch, so rewinding past a write undoes it on screen."""
    script = [
        _write("t1", ("First plan", "in_progress")),
        ScriptedTurn(content="turn one done"),
        _write("t2", ("Second plan", "in_progress")),
        ScriptedTurn(content="turn two done"),
    ]
    async with boot(script, manifests={"e2e-todo": _agent()}) as app:
        await _stream(app, "one")
        await _stream(app, "two")
        assert [t["content"] for t in await _snapshot_todos(app)] == ["Second plan"]

        snapshot = (await app.client.get(f"/chat/sessions/{THREAD}")).json()
        [end_of_one] = [i for i in snapshot["transcript"] if i.get("content") == "turn one done"]
        event_id = end_of_one["metadata"]["event_id"]
        resp = await app.client.post("/chat/rewind", json={"thread_id": THREAD, "event_id": event_id})
        assert resp.status_code == 200, resp.text
        assert [t["content"] for t in await _snapshot_todos(app)] == ["First plan"]


async def test_it_works_on_a_turn_with_no_thread(boot: Any) -> None:
    script = [_write("t1", ("A", "pending")), ScriptedTurn(content="ok")]
    async with boot(script, manifests={"e2e-todo": _agent()}) as app:
        resp = await app.client.post(
            "/v1/chat/completions",
            json={"model": "e2e-todo", "messages": [{"role": "user", "content": "go"}]},
        )
        assert resp.status_code == 200, resp.text
        assert "Todo list updated: 0/1 completed." in _text(app.spy.prompts[1])


async def test_a_write_governance_refused_is_not_the_list(boot: Any) -> None:
    """Valid arguments, refused by `limits`: the call is in the transcript, but it changed
    nothing, so the snapshot keeps the list the last *successful* write left."""
    script = [
        _write("t1", ("Kept", "in_progress")),
        _write("t2", ("Never written", "in_progress")),
        ScriptedTurn(content="out of calls"),
    ]
    async with boot(script, manifests={"e2e-todo": _agent(limits={"max_tool_calls": 1})}) as app:
        await _stream(app)
        assert "max_tool_calls (1) exceeded" in _text(app.spy.prompts[2])
        assert [t["content"] for t in await _snapshot_todos(app)] == ["Kept"]
