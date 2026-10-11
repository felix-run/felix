"""`PUT /plans/{id}`: an operator's edit to an agent's plan says so, is audited, and is told.

The route is how a person writes a plan; the agent writes through its plan tools. An edit made
here used to be stored exactly as the agent's own, with nothing to say a person wrote it and
nothing to tell the agent its plan had moved, so a client showing it would present steps the
agent never wrote as the agent's. Now the plan carries `edited` (by whom, when), the edit is a
`plan_edit` audit event, and the plan's own conversation reads a note before its next call.
"""

from __future__ import annotations

from typing import Any

from felix.flush import flush_all
from felix.plans import store as plans_store
from felix_ai.providers.scripted import ScriptedTurn

from tests.support.e2e import Booted


async def _seed(app: Booted, thread: str, text: str = "make a plan") -> None:
    resp = await app.client.post(
        "/chat",
        json={"manifest": "quick", "thread_id": thread, "messages": [{"role": "user", "content": text}]},
    )
    assert resp.status_code == 200, resp.text


async def _agent_plan(app: Booted, plan_id: str, thread: str) -> dict[str, Any]:
    """A plan as the agent's `plan_create` leaves it, in `thread`."""
    return await plans_store.put_plan(
        app.settings,
        "default",
        plan_id,
        plan={
            "title": "Ship it",
            "goal": "",
            "steps": [{"id": "1", "title": "Write", "status": "done"}],
            "status": "active",
        },
        thread_id=f"default:{thread}" if thread else "",
    )


async def test_an_edit_is_marked_audited_and_read_by_the_next_turn(boot: Any) -> None:
    thread = "e2e-plan-edit"
    async with boot([ScriptedTurn(content="planned"), ScriptedTurn(content="carrying on")]) as app:
        await _seed(app, thread)
        stored = await _agent_plan(app, "p-edit", thread)
        resp = await app.client.put(
            "/plans/p-edit",
            json={
                "plan": {
                    **stored["plan"],
                    "steps": [{"id": "1", "title": "Write it again", "status": "pending"}],
                    # A client cannot say who edited: this is replaced.
                    "edited": {"by": "the agent", "at": 1},
                },
                "expected_updated_at": stored["updated_at"],
            },
        )
        assert resp.status_code == 200, resp.text
        out = resp.json()
        assert out["agent_told"] == "recorded", out
        edited = out["plan"]["edited"]
        assert edited["by"] != "the agent" and edited["at"] > 1, edited

        await flush_all(app.settings)
        audit = (await app.client.get("/audit", params={"event_type": "plan_edit"})).json()["items"]
        assert [e["payload_json"] for e in audit] == [
            {"plan_id": "p-edit", "thread_id": f"default:{thread}"}
        ], audit

        snap = (await app.client.get(f"/chat/sessions/{thread}")).json()
        note = next(e for e in snap["transcript"] if e["kind"] == "custom")
        assert note["metadata"]["kind"] == "plan" and note["metadata"]["path"] == "p-edit", note
        assert "`p-edit`" in note["content"] and "plan_get" in note["content"], note["content"]
        # The id only: the operator's title is prose, and never reaches the model as a note.
        assert "Ship it" not in note["content"]

        await _seed(app, thread, "carry on")
        prompt = app.spy.prompts[-1]
        mentions = [m for m in prompt if "p-edit" in str(getattr(m, "content", ""))]
        assert len(mentions) == 1, "the next turn did not read the note exactly once"


async def test_a_plan_written_outside_a_chat_is_marked_and_tells_no_one(boot: Any) -> None:
    async with boot([]) as app:
        stored = await _agent_plan(app, "p-loose", "")
        resp = await app.client.put(
            "/plans/p-loose",
            json={"plan": stored["plan"], "expected_updated_at": stored["updated_at"]},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["agent_told"] is None
        assert resp.json()["plan"]["edited"]["by"], resp.json()


async def test_a_stale_edit_is_refused_and_nothing_is_said(boot: Any) -> None:
    thread = "e2e-plan-stale"
    async with boot([ScriptedTurn(content="planned")]) as app:
        await _seed(app, thread)
        stored = await _agent_plan(app, "p-stale", thread)
        resp = await app.client.put(
            "/plans/p-stale",
            json={"plan": stored["plan"], "expected_updated_at": stored["updated_at"] - 1},
        )
        assert resp.status_code == 409, resp.text
        snap = (await app.client.get(f"/chat/sessions/{thread}")).json()
        assert not [e for e in snap["transcript"] if e["kind"] == "custom"], "a refused edit was told"


async def test_the_agents_own_step_update_keeps_the_mark(boot: Any) -> None:
    """`plan_update_step` copies the stored blob, so an edited plan stays marked as edited."""
    async with boot([]) as app:
        stored = await _agent_plan(app, "p-keep", "")
        await app.client.put(
            "/plans/p-keep",
            json={"plan": stored["plan"], "expected_updated_at": stored["updated_at"]},
        )
        row = await plans_store.get_plan(app.settings, "default", "p-keep")
        assert row is not None
        plan = dict(row["plan"])
        plan["steps"] = [{**plan["steps"][0], "status": "pending"}]
        await plans_store.put_plan(
            app.settings, "default", "p-keep", plan=plan, expected_updated_at=row["updated_at"]
        )
        after = await plans_store.get_plan(app.settings, "default", "p-keep")
        assert after is not None and after["plan"]["edited"] == row["plan"]["edited"]
