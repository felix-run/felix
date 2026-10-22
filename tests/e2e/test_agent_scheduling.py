"""An agent schedules follow-up work, a person approves it, and the task fires as that person.

The chain no unit test holds end to end: `spec.scheduling` → the builder binding `schedule_task`
before the governance stack, and the default approval rule gating it → a job row the jobs routes
list as the caller's → `POST /jobs/{name}/run` firing it through the same path cron takes, as
`cron` on the caller's behalf → the scripted model reading the task's prompt, and a firing that
tries to schedule more being refused.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from felix.manifests.loader import parse_manifest
from felix_ai.providers.scripted import ScriptedTurn
from felix_ai.types import ToolCall

KEYS = {"sk-e2e-alice": {"tenant_id": "default", "sub": "alice", "scopes": ["jobs:write", "approvals:write"]}}
ENV = {"FELIX_AUTH_MODE": "api_key", "FELIX_AUTH_API_KEYS": json.dumps(KEYS)}
ALICE = {"Authorization": "Bearer sk-e2e-alice"}
PROMPT = "Check whether release 1.4 has shipped and say so."


def _manifest(**scheduling: Any) -> Any:
    return parse_manifest(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": "e2e-sched"},
            "spec": {"pattern": "react", "scheduling": scheduling},
        }
    )


def _when() -> str:
    # Real clock: the store refuses a one-shot in the past, and the e2e path has no clock seam.
    at = (datetime.now(UTC) + timedelta(days=1)).replace(microsecond=0)
    return "at:" + at.isoformat().replace("+00:00", "Z")


def _schedule_call(call_id: str, when: str) -> ScriptedTurn:
    args = {"when": when, "prompt": PROMPT, "title": "release check"}
    return ScriptedTurn(tool_calls=[ToolCall(id=call_id, name="schedule_task", args=args)])


def _tool_result(prompt: list[Any]) -> dict[str, Any]:
    return json.loads(next(str(m.content) for m in reversed(prompt) if getattr(m, "role", "") == "tool"))


async def _chat(app: Any, text: str) -> Any:
    return await app.client.post(
        "/chat",
        headers=ALICE,
        json={
            "manifest": "e2e-sched",
            "thread_id": "e2e-sched",
            "messages": [{"role": "user", "content": text}],
        },
    )


async def _tasks(app: Any) -> list[dict[str, Any]]:
    listed = await app.client.get("/jobs", headers=ALICE)
    assert listed.status_code == 200, listed.text
    return listed.json()["items"]


async def test_a_task_fires_as_the_person_who_scheduled_it_and_cannot_schedule_more(boot: Any) -> None:
    when = _when()
    script = [
        _schedule_call("c1", when),
        ScriptedTurn(content="scheduled"),
        # The firing: it tries to schedule more, is refused, and answers.
        _schedule_call("c2", when),
        ScriptedTurn(content="not shipped yet"),
    ]
    async with boot(script, env=ENV, manifests={"e2e-sched": _manifest(approval=False)}) as app:
        resp = await _chat(app, "Check on the release tomorrow.")
        assert resp.status_code == 200, resp.text
        scheduled = _tool_result(app.spy.prompts[1])
        assert scheduled["status"] == "scheduled", scheduled

        (job,) = await _tasks(app)
        assert (job["name"], job["schedule"], job["manifest_id"]) == (scheduled["name"], when, "e2e-sched")
        assert job["created_by"] == "alice" and isinstance(job["recorded_at"], int)
        assert job["payload"]["scheduled_by"]["thread_id"] == "default:e2e-sched"

        fired = await app.client.post(f"/jobs/{job['name']}/run", headers=ALICE)
        assert fired.status_code == 200, fired.text
        assert fired.json()["status"] == "ok", fired.json()
        assert fired.json()["result"]["answer"] == "not shipped yet"

        # The task's prompt is what the firing's model was handed, as a user turn.
        assert PROMPT in "\n".join(str(m.content) for m in app.spy.prompts[2] if m.role == "user")
        refused = _tool_result(app.spy.prompts[3])
        assert refused["error"] == "scheduled_run"
        assert len(await _tasks(app)) == 1


async def test_schedule_task_waits_for_a_person_by_default(boot: Any) -> None:
    script = [_schedule_call("c1", _when()), ScriptedTurn(content="scheduled")]
    async with boot(script, env=ENV, manifests={"e2e-sched": _manifest()}) as app:
        chat = asyncio.create_task(_chat(app, "Check on the release tomorrow."))
        try:
            pending: list[dict[str, Any]] = []
            for _ in range(100):  # the gate writes its row a moment after the call starts
                await asyncio.sleep(0.05)
                listing = await app.client.get(
                    "/approvals", headers=ALICE, params={"thread_id": "default:e2e-sched"}
                )
                assert listing.status_code == 200, listing.text
                pending = listing.json()["items"]
                if pending:
                    break
            assert pending, "schedule_task is held for approval without the manifest naming a rule"
            assert (pending[0]["tool_name"], pending[0]["rule_id"]) == (
                "schedule_task",
                "scheduling.schedule_task",
            )
            assert await _tasks(app) == [], "nothing is scheduled while the call waits"

            decided = await app.client.post(
                f"/approvals/{pending[0]['id']}/decide", headers=ALICE, json={"decision": "approved"}
            )
            assert decided.status_code == 200, decided.text
            resp = await asyncio.wait_for(chat, timeout=20)
            assert resp.status_code == 200, resp.text
            (job,) = await _tasks(app)
            assert job["created_by"] == "alice" and job["payload"]["prompt"] == PROMPT
        finally:
            # A failed assertion above must not leave the request blocked on the approval.
            chat.cancel()


FOLLOW = {
    "name": "Follow up",
    "inputs": {},
    "steps": [
        {"id": "ask", "kind": "agent_turn", "config": {"manifest": "e2e-sched", "prompt": "Follow up."}}
    ],
    "edges": [],
}
WF_KEYS = {
    "sk-e2e-alice": {
        "tenant_id": "default",
        "sub": "alice",
        "scopes": ["jobs:write", "workflows:write", "workflows:read", "approvals:write"],
    }
}
WF_ENV = {"FELIX_AUTH_MODE": "api_key", "FELIX_AUTH_API_KEYS": json.dumps(WF_KEYS)}


async def test_a_workflow_a_job_starts_cannot_schedule_more(boot: Any) -> None:
    """A job naming a workflow starts a run whose agent steps run as `fiber`, not `cron`; they
    still descend from a scheduled firing, and the marker that says so travels with the run's
    recorded starter into each step."""
    from tests.support.e2e import tick_workflow_run

    script = [_schedule_call("c1", _when()), ScriptedTurn(content="followed up")]
    async with boot(script, env=WF_ENV, manifests={"e2e-sched": _manifest(approval=False)}) as app:
        saved = await app.client.put("/workflows/follow", json=FOLLOW, headers=ALICE)
        assert saved.status_code == 201, saved.text
        job = {"schedule": "", "workflow_id": "follow", "payload": {"inputs": {}}}
        written = await app.client.put("/jobs/nightly", json=job, headers=ALICE)
        assert written.status_code == 200, written.text

        fired = await app.client.post("/jobs/nightly/run", headers=ALICE)
        assert fired.status_code == 200 and fired.json()["status"] == "ok", fired.text
        run_url = f"/workflows/follow/runs/{fired.json()['result']['workflow_run_id']}"
        done = await tick_workflow_run(app, run_url, {"completed", "failed"}, headers=ALICE)

        assert done["status"] == "completed", done
        refused = _tool_result(app.spy.prompts[1])
        assert refused["error"] == "scheduled_run", refused
        assert [j["name"] for j in await _tasks(app)] == ["nightly"], "no task was written"
