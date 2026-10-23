"""An agent builds a workflow for a person, the person approves it, and the agent runs it.

The chain no unit test holds end to end: `spec.workflow_authoring` → the builder binding the tools
before the governance stack, with the default approval rule gating `save_workflow` → a workflow the
`/workflows` routes list as the person's, marked as this agent's → `run_workflow` starting a run as
them → a person's save through the routes taking the workflow over, after which the agent may no
longer change it.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from felix.manifests.loader import parse_manifest
from felix_ai.providers.scripted import ScriptedTurn
from felix_ai.types import ToolCall

SCOPES = ["workflows:write", "approvals:write"]
KEYS = {"sk-e2e-alice": {"tenant_id": "default", "sub": "alice", "scopes": SCOPES}}
ENV = {"FELIX_AUTH_MODE": "api_key", "FELIX_AUTH_API_KEYS": json.dumps(KEYS)}
ALICE = {"Authorization": "Bearer sk-e2e-alice"}
NAME = "e2e-flows"
DEFINITION = {
    "name": "Route a label",
    "inputs": {"label": {"type": "string"}},
    "steps": [{"id": "route", "kind": "branch", "config": {"cases": ["bug", "feature"]}}],
    "edges": [{"source": {"step": "trigger", "port": "label"}, "target": {"step": "route", "port": "value"}}],
}


def _manifest() -> Any:
    return parse_manifest(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": NAME},
            "spec": {"workflow_authoring": {}},
        }
    )


def _call(call_id: str, name: str, args: dict[str, Any]) -> ScriptedTurn:
    return ScriptedTurn(tool_calls=[ToolCall(id=call_id, name=name, args=args)])


def _tool_results(app: Any) -> list[dict[str, Any]]:
    """Every tool result the model was handed, in order, read from the last prompt it saw."""
    return [json.loads(str(m.content)) for m in app.spy.prompts[-1] if getattr(m, "role", "") == "tool"]


async def _chat(app: Any, text: str) -> Any:
    return await app.client.post(
        "/chat",
        headers=ALICE,
        json={"manifest": NAME, "thread_id": NAME, "messages": [{"role": "user", "content": text}]},
    )


async def _pending(app: Any) -> list[dict[str, Any]]:
    for _ in range(100):  # the gate writes its row a moment after the call starts
        await asyncio.sleep(0.05)
        listing = await app.client.get("/approvals", headers=ALICE, params={"thread_id": f"default:{NAME}"})
        assert listing.status_code == 200, listing.text
        if listing.json()["items"]:
            return listing.json()["items"]
    return []


async def test_an_approved_workflow_is_the_persons_runs_as_them_and_a_console_save_adopts_it(
    boot: Any,
) -> None:
    script = [
        _call("c1", "check_workflow", {"definition": DEFINITION}),
        _call("c2", "save_workflow", {"definition": DEFINITION}),
        ScriptedTurn(content="saved"),
    ]
    async with boot(script, env=ENV, manifests={NAME: _manifest()}) as app:
        chat = asyncio.create_task(_chat(app, "Build me a workflow that routes issue labels."))
        try:
            pending = await _pending(app)
            assert [(p["tool_name"], p["rule_id"]) for p in pending] == [
                ("save_workflow", "workflow_authoring.save_workflow")
            ]
            assert (await app.client.get("/workflows", headers=ALICE)).json()["items"] == [], (
                "nothing is saved while the call waits"
            )
            decided = await app.client.post(
                f"/approvals/{pending[0]['id']}/decide", headers=ALICE, json={"decision": "approved"}
            )
            assert decided.status_code == 200, decided.text
            resp = await asyncio.wait_for(chat, timeout=20)
            assert resp.status_code == 200, resp.text
        finally:
            chat.cancel()

        checked, saved = _tool_results(app)
        assert checked["ok"] is True, checked
        assert saved["status"] == "created", saved
        (listed,) = (await app.client.get("/workflows", headers=ALICE)).json()["items"]
        assert (listed["id"], listed["authored_via"]) == (saved["workflow_id"], f"agent:{NAME}")
        assert listed["created_by"].endswith("|alice"), "the owner is issuer-qualified"

        app.spy.queue.extend(
            [
                _call(
                    "c3",
                    "run_workflow",
                    {
                        "workflow_id": saved["workflow_id"],
                        "inputs": {"label": "bug"},
                        "revision": saved["revision"],
                    },
                ),
                ScriptedTurn(content="started"),
            ]
        )
        assert (await asyncio.wait_for(_chat(app, "Run it on a bug."), timeout=20)).status_code == 200
        started = _tool_results(app)[-1]
        assert "run_id" in started, started
        runs = (await app.client.get(f"/workflows/{saved['workflow_id']}/runs", headers=ALICE)).json()[
            "items"
        ]
        assert [(r["id"], r["trigger"], r["started_by"]) for r in runs] == [
            (started["run_id"], f"agent:{NAME}", "alice")
        ]

        # Alice edits it in the console: the agent may still run it for her, but not change it.
        put = await app.client.put(
            f"/workflows/{saved['workflow_id']}", headers=ALICE, json={**DEFINITION, "name": "Reviewed"}
        )
        assert put.status_code == 200 and put.json()["authored_via"] == f"owner:agent:{NAME}", put.text
        app.spy.queue.extend(
            [
                _call("c4", "save_workflow", {"definition": DEFINITION, "workflow_id": saved["workflow_id"]}),
                ScriptedTurn(content="refused"),
            ]
        )
        assert (await asyncio.wait_for(_chat(app, "Change it back."), timeout=20)).status_code == 200
        # Refused by the tool's own checks, which the approval gate runs first: no one is asked.
        last = next(str(m.content) for m in reversed(app.spy.prompts[-1]) if getattr(m, "role", "") == "tool")
        assert last.startswith("[approval preview failed]") and "edited_by_person" in last, last
        listing = await app.client.get("/approvals", headers=ALICE, params={"thread_id": f"default:{NAME}"})
        assert [p["status"] for p in listing.json()["items"]] in ([], ["approved"]), listing.json()
