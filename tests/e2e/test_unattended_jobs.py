"""A scheduled job reaching an approval-gated tool is refused at once, not parked on nobody.

Through the production app: a job row written over HTTP, fired by `POST /jobs/{name}/run` or the
worker's sweep (`run_due_jobs_all_tenants`), compiled with the real governance stack, the model
scripted. Before this, the firing wrote a pending approval, pushed it to the tenant, and waited
out the rule's ttl -- and every job after it in the sweep waited too.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest
from felix.manifests.loader import parse_manifest
from felix_ai.providers.scripted import ScriptedTurn
from felix_ai.types import ToolCall

KEYS = {
    "sk-e2e-alice": {
        "tenant_id": "default",
        "sub": "alice",
        "scopes": ["jobs:write", "approvals:write", "workflows:write", "audit:read"],
    }
}
ENV = {"FELIX_AUTH_MODE": "api_key", "FELIX_AUTH_API_KEYS": json.dumps(KEYS)}
ALICE = {"Authorization": "Bearer sk-e2e-alice"}
REFUSAL = (
    "[approval unavailable] tool=calculator rule=calc-gate: this run is unattended "
    "(no person started it), so no one can approve it; the call was not made."
)


def _manifest(name: str, *, gate: dict[str, Any] | None = None) -> Any:
    spec: dict[str, Any] = {"pattern": "react", "auth": {"inbound": {"allow_anonymous": True}}}
    if gate is not None:
        spec["tools"] = ["calculator"]
        spec["approvals"] = [{"id": "calc-gate", "tools": ["calculator"], **gate}]
    return parse_manifest(
        {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": name}, "spec": spec}
    )


MANIFESTS = {
    # A ttl no test could wait out: a run that waits on it fails the test's own time bound.
    "gated": _manifest("gated", gate={"ttl_seconds": 300}),
    "patient": _manifest("patient", gate={"ttl_seconds": 1, "allow_unattended": True}),
    "plain": _manifest("plain"),
}
CALC = ScriptedTurn(tool_calls=[ToolCall(id="c1", name="calculator", args={"expression": "1+1"})])


@pytest.fixture
def pushed(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    from felix.push import notify

    calls: list[str] = []
    monkeypatch.setattr(notify, "approval_pending", lambda _settings, row: calls.append(str(row["id"])))
    return calls


async def _put_job(app: Any, name: str, body: dict[str, Any]) -> None:
    written = await app.client.put(f"/jobs/{name}", json=body, headers=ALICE)
    assert written.status_code == 200, written.text


async def _approvals(app: Any) -> list[dict[str, Any]]:
    """Every approval row, whatever became of it: one asked and timed out is `denied` by then."""
    items: list[dict[str, Any]] = []
    for status in ("pending", "approved", "denied"):
        listed = await app.client.get("/approvals", headers=ALICE, params={"status": status})
        assert listed.status_code == 200, listed.text
        items += listed.json()["items"]
    return items


def _tool_text(prompt: list[Any]) -> str:
    return next(str(m.content) for m in reversed(prompt) if getattr(m, "role", "") == "tool")


async def test_a_job_reaching_a_gated_tool_is_refused_at_once(boot: Any, pushed: list[str]) -> None:
    from felix.audit import store as audit_store

    async with boot([CALC, ScriptedTurn(content="done")], env=ENV, manifests=MANIFESTS) as app:
        await _put_job(app, "calc", {"manifest_id": "gated", "payload": {"prompt": "add"}})
        started = time.monotonic()
        fired = await asyncio.wait_for(app.client.post("/jobs/calc/run", headers=ALICE), timeout=10)
        took = time.monotonic() - started
        assert fired.status_code == 200, fired.text
        assert (fired.json()["status"], fired.json()["result"]["answer"]) == ("ok", "done")
        refused = _tool_text(app.spy.prompts[1])
        approvals = await _approvals(app)
        await audit_store.flush_pending(app.settings)
        denies, _ = await audit_store.list_events(app.settings, "default", event_type="policy_deny")
        job = (await app.client.get("/jobs/calc", headers=ALICE)).json()

    assert took < 5, f"the firing waited {took:.1f}s"
    assert refused == REFUSAL
    assert approvals == [], "no approval row for a run no one can approve"
    assert pushed == [], "nobody was pushed a request they could not usefully answer"
    assert [(e["payload_json"]["tool"], e["payload_json"]["control"]) for e in denies] == [
        ("calculator", "approvals")
    ]
    assert job["last_status"] == "ok"


async def test_allow_unattended_still_asks_and_waits(boot: Any, pushed: list[str]) -> None:
    async with boot([CALC, ScriptedTurn(content="done")], env=ENV, manifests=MANIFESTS) as app:
        await _put_job(app, "calc", {"manifest_id": "patient", "payload": {"prompt": "add"}})
        started = time.monotonic()
        fired = await asyncio.wait_for(app.client.post("/jobs/calc/run", headers=ALICE), timeout=15)
        took = time.monotonic() - started
        assert fired.status_code == 200, fired.text
        timed_out = _tool_text(app.spy.prompts[1])
        (row,) = await _approvals(app)

    assert took >= 0.9, "it did not wait for the rule's ttl"
    assert timed_out == "[approval timeout] tool=calculator rule=calc-gate"
    assert (row["tool_name"], row["principal_subj"], row["thread_id"]) == (
        "calculator",
        "alice",
        "default:job:calc",
    )
    assert pushed == [row["id"]]


async def test_a_gated_job_does_not_hold_up_the_next_one_in_the_sweep(boot: Any) -> None:
    """Both due at once; the sweep fires them in name order and awaits each. The first used to sit
    on its approval for the rule's ttl before the second was looked at."""
    from felix.jobs import store as jobs_store
    from felix.jobs.scheduler import run_due_jobs_all_tenants

    script = [CALC, ScriptedTurn(content="done"), ScriptedTurn(content="plain done")]
    async with boot(script, env=ENV, manifests=MANIFESTS) as app:
        every_minute = {"schedule": "* * * * *", "payload": {"prompt": "go"}}
        await _put_job(app, "a-gated", {**every_minute, "manifest_id": "gated"})
        await _put_job(app, "b-plain", {**every_minute, "manifest_id": "plain"})
        assert await asyncio.wait_for(run_due_jobs_all_tenants(app.settings), timeout=10) == 2
        (second,) = await jobs_store.list_runs(app.settings, "default", "b-plain")
        (first,) = await jobs_store.list_runs(app.settings, "default", "a-gated")
    assert (first["status"], second["status"]) == ("ok", "ok")
    assert second["result"]["answer"] == "plain done"


async def test_a_workflow_jobs_agent_step_is_refused_too(boot: Any, pushed: list[str]) -> None:
    """The job starts a workflow run; its agent step runs later in a child fiber, as `fiber` for the
    writer. The firing's mark rides the recorded caller into that step, so the step is unattended."""
    from tests.support.e2e import tick_workflow_run

    definition = {
        "name": "Calc",
        "inputs": {"sum": {"type": "string"}},
        "steps": [
            {"id": "work", "kind": "agent_turn", "config": {"manifest": "gated", "prompt": "Add {{ sum }}"}}
        ],
        "edges": [{"source": {"step": "trigger", "port": "sum"}, "target": {"step": "work", "port": "sum"}}],
    }
    async with boot([CALC, ScriptedTurn(content="done")], env=ENV, manifests=MANIFESTS) as app:
        assert (await app.client.put("/workflows/calc", json=definition, headers=ALICE)).status_code == 201
        await _put_job(app, "wf", {"workflow_id": "calc", "payload": {"inputs": {"sum": "1+1"}}})
        fired = await app.client.post("/jobs/wf/run", headers=ALICE)
        assert fired.status_code == 200 and fired.json()["status"] == "ok", fired.text
        run_url = f"/workflows/calc/runs/{fired.json()['result']['workflow_run_id']}"
        done = await asyncio.wait_for(
            tick_workflow_run(app, run_url, {"completed", "failed"}, headers=ALICE), timeout=20
        )
        refused = _tool_text(app.spy.prompts[1])
        approvals = await _approvals(app)

    assert done["status"] == "completed", done
    assert done["steps"]["work"]["outputs"] == {"text": "done"}
    assert refused == REFUSAL
    assert approvals == [] and pushed == []


async def test_a_manual_run_is_refused_while_the_job_is_running(boot: Any) -> None:
    from felix.jobs import store as jobs_store
    from felix.jobs.scheduler import now_ms

    async with boot([ScriptedTurn(content="ran")], env=ENV, manifests=MANIFESTS) as app:
        await _put_job(app, "slow", {"manifest_id": "plain", "payload": {"prompt": "go"}})
        assert await jobs_store.claim_manual_run(app.settings, "default", "slow", last_run_at=now_ms())
        busy = await app.client.post("/jobs/slow/run", headers=ALICE)
        assert (busy.status_code, busy.json()["detail"]) == (409, {"error": "job_running"})
        assert await jobs_store.list_runs(app.settings, "default", "slow") == [], "nothing ran"
        await jobs_store.touch_run(
            app.settings, "default", "slow", last_run_at=now_ms(), next_run_at=jobs_store.KEEP_SCHEDULE
        )
        ran = await app.client.post("/jobs/slow/run", headers=ALICE)
        missing = await app.client.post("/jobs/nope/run", headers=ALICE)
    assert ran.status_code == 200 and ran.json()["result"]["answer"] == "ran", ran.text
    assert missing.status_code == 404
