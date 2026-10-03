"""A durable run re-claimed after a crash continues from its session log.

A worker that dies mid-invoke leaves the fiber at the same cursor, and the next claim ran the
whole invoke again: the user turn was re-sent onto a thread that already held the run's model
turns and tool results, so the model answered the request a second time and could repeat
tool calls that had already taken effect. The log is the journal — each turn is appended as it
happens — so these crash a real run part-way and check what the next claim does.

The crash is a `BaseException` raised from the model: it passes every `except Exception` on
the way out, the way a killed worker takes nothing with it, and leaves the fiber leased at the
same cursor with whatever the loop had logged.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.manifests.loader import parse_manifest
from felix_ai.providers.scripted import ScriptedTurn
from felix_ai.types import ToolCall

REQUEST = "what is 2+2?"
CALC = ToolCall(id="call-1", name="calculator", args={"expression": "2+2"})


class WorkerDied(BaseException):
    """What a SIGKILL looks like from inside the step: nothing after it runs."""


def _manifest(name: str = "e2e-durable", **spec: Any) -> Any:
    base = {
        "pattern": "react",
        "tools": ["calculator"],
        "auth": {"inbound": {"allow_anonymous": True}},
        "execution": {"mode": "durable"},
        **spec,
    }
    return parse_manifest(
        {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": name}, "spec": base}
    )


async def _enqueue(app: Any, manifest: str = "e2e-durable") -> str:
    resp = await app.client.post(
        "/chat", json={"manifest": manifest, "messages": [{"role": "user", "content": REQUEST}]}
    )
    assert resp.status_code == 202, resp.text
    return resp.json()["resume_token"]


async def _crash(app: Any, token: str) -> None:
    """Run the fiber until the scripted crash, then let its lease lapse."""
    from felix.durability import fibers as F

    with pytest.raises(WorkerDied):
        await F.resume_due_fibers(app.settings)
    F._memory_fibers[("default", token)]["lease_until"] = 0


async def _thread(token: str) -> str:
    from felix.durability import fibers as F

    step = F._memory_fibers[("default", token)]["state_json"]["steps"][0]
    return step.get("thread_id") or F.fiber_thread_id("default", token)


def _users(prompt: list[Any]) -> list[str]:
    return [str(m.content) for m in prompt if m.role == "user"]


async def test_a_run_killed_after_its_tool_call_continues_without_repeating_it(boot: Any) -> None:
    from felix.durability.fibers import resume_due_fibers

    script = [
        ScriptedTurn(content="", tool_calls=[CALC], stop_reason="tool_use"),
        ScriptedTurn(error=WorkerDied()),
        ScriptedTurn(content="It is 4."),
    ]
    async with boot(script, manifests={"e2e-durable": _manifest()}) as app:
        token = await _enqueue(app)
        await _crash(app, token)
        await resume_due_fibers(app.settings)
        run = (await app.client.get(f"/chat/runs/{token}")).json()
        prompts = app.spy.prompts

    assert run["status"] == "completed" and run["final"]["content"] == "It is 4."
    assert len(prompts) == 3, "one call before the crash, the crashed one, and one after"
    assert _users(prompts[-1]) == [REQUEST], "the request is not sent a second time"
    assert [m.content for m in prompts[-1] if m.role == "tool"] == ["4"], (
        "the tool ran once; its result stands"
    )


async def test_a_run_whose_reply_was_logged_is_not_run_again(boot: Any) -> None:
    """Killed after the answer was logged and before the fiber's save: the answer stands.

    The memory twin keeps the claimed row as the stored one, so a save that "dies" would still
    leave the step's in-place edits behind; the stored row is put back to what a lost write
    leaves on Postgres — the pre-step row plus the marker the step checkpointed.
    """
    import copy

    from felix.durability import fibers as F
    from felix.durability.fibers import resume_due_fibers
    from felix.session.store import get_session_store

    script = [ScriptedTurn(content="It is 4."), ScriptedTurn(content="a second answer")]
    async with boot(script, manifests={"e2e-durable": _manifest()}) as app:
        token = await _enqueue(app)
        key = ("default", token)
        session = get_session_store(app.settings, tenant_id="default").open(await _thread(token))
        began = {"cursor": 0, "seq": int((await session.head())["seq"])}
        before = copy.deepcopy(F._memory_fibers[key])
        real = F._save_fiber

        async def dies_at_the_closing_save(settings: Any, row: dict[str, Any], **kw: Any) -> None:
            if (row.get("state_json") or {}).get("stash", {}).get("last"):
                lost = copy.deepcopy(before)
                lost["state_json"]["invoke_began"] = began
                F._memory_fibers[key] = lost
                raise WorkerDied
            await real(settings, row, **kw)

        F._save_fiber = dies_at_the_closing_save
        try:
            await _crash(app, token)
        finally:
            F._save_fiber = real
        await resume_due_fibers(app.settings)
        run = (await app.client.get(f"/chat/runs/{token}")).json()
        calls = len(app.spy.prompts)

    assert calls == 1, "no model call on the re-claim"
    assert run["status"] == "completed" and run["final"]["content"] == "It is 4."


async def test_a_run_killed_before_its_turn_was_logged_sends_it(boot: Any) -> None:
    """Events the loop writes ahead of the user turn are not the turn: a model change here."""
    from felix.durability import fibers as F
    from felix.durability.fibers import resume_due_fibers
    from felix.session.store import get_session_store
    from felix.session.types import AppendableEvent

    async with boot([ScriptedTurn(content="It is 4.")], manifests={"e2e-durable": _manifest()}) as app:
        token = await _enqueue(app)
        session = get_session_store(app.settings, tenant_id="default").open(await _thread(token))
        row = F._memory_fibers[("default", token)]
        head = int((await session.head())["seq"])
        row["state_json"] = {**row["state_json"], "invoke_began": {"cursor": 0, "seq": head}}
        await session.append(AppendableEvent(kind="model_change", content="claude-sonnet-5"))
        await resume_due_fibers(app.settings)
        [prompt] = app.spy.prompts

    assert _users(prompt) == [REQUEST], "the request is sent, not skipped"


async def test_a_router_is_not_resumed_without_its_request(boot: Any) -> None:
    """A router picks its child from the incoming turn. Resumed with none, it would route to its
    first child whatever the request was, so a composite keeps the old re-send."""
    from felix.durability.fibers import resume_due_fibers

    manifests = {
        "e2e-a": _manifest("e2e-a", execution={}),
        "e2e-b": _manifest("e2e-b", execution={}),
        "e2e-router": _manifest("e2e-router", pattern="router", sub_agents=["e2e-a", "e2e-b"], tools=[]),
    }
    script = [
        ScriptedTurn(content="e2e-b"),
        ScriptedTurn(content="", tool_calls=[CALC], stop_reason="tool_use"),
        ScriptedTurn(error=WorkerDied()),
        ScriptedTurn(content="e2e-b"),
        ScriptedTurn(content="It is 4."),
    ]
    async with boot(script, manifests=manifests) as app:
        token = await _enqueue(app, "e2e-router")
        await _crash(app, token)
        await resume_due_fibers(app.settings)
        prompts = app.spy.prompts

    assert len(prompts) == 5
    assert REQUEST in " ".join(str(m.content) for m in prompts[3]), "the router classifies the request again"


async def test_a_claim_lost_before_the_invoke_calls_no_model(boot: Any) -> None:
    """The marker is written under the claim's version; refused, the row is someone else's."""
    from felix.durability import fibers as F
    from felix.durability.fibers import resume_due_fibers

    async with boot([ScriptedTurn(content="It is 4.")], manifests={"e2e-durable": _manifest()}) as app:
        token = await _enqueue(app)
        real = F._checkpoint_state

        async def refused(settings: Any, row: dict[str, Any]) -> bool:
            return False

        F._checkpoint_state = refused
        try:
            await resume_due_fibers(app.settings)
        finally:
            F._checkpoint_state = real
        calls = len(app.spy.prompts)
        row = F._memory_fibers[("default", token)]

    assert calls == 0
    assert row["status"] != "completed" and int(row["state_json"].get("cursor") or 0) == 0
