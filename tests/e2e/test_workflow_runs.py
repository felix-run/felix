"""A workflow run end to end: the production app over HTTP, a scripted model, the fiber loop.

No worker runs in tests, so `_tick` does what `run_fiber_loop` would: it wakes every parked fiber
and claims what is due, until the run reaches the status a test waits for. The orchestrator and
each agent step's child run are real fibers in the memory store; the model is scripted.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.manifests.loader import parse_manifest
from felix_ai.providers.scripted import ScriptedTurn

LABEL = {"type": "string", "enum": ["bug", "feature"]}
CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {"label": LABEL, "summary": {"type": "string"}},
    "required": ["label", "summary"],
}


def _manifest(**spec: Any) -> Any:
    return parse_manifest(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": "e2e-wf"},
            "spec": {"pattern": "react", "tools": [], "auth": {"inbound": {"allow_anonymous": True}}, **spec},
        }
    )


def _edge(src: str, src_port: str, dst: str, dst_port: str) -> dict[str, Any]:
    return {"source": {"step": src, "port": src_port}, "target": {"step": dst, "port": dst_port}}


def _triage() -> dict[str, Any]:
    """issue → classify → route: bug → approve → fix; feature → note (never taken here)."""
    return {
        "name": "Triage",
        "inputs": {"issue": {"type": "string"}},
        "steps": [
            {
                "id": "classify",
                "kind": "agent_turn",
                "config": {
                    "manifest": "e2e-wf",
                    "prompt": "Classify: {{ issue }}",
                    "output_schema": CLASSIFY_SCHEMA,
                },
            },
            {"id": "route", "kind": "branch", "config": {"cases": ["bug", "feature"]}},
            {"id": "approve", "kind": "approval", "config": {"prompt": "Fix this bug?"}},
            {
                "id": "fix",
                "kind": "agent_turn",
                "config": {"manifest": "e2e-wf", "prompt": "Fix: {{ summary }}"},
            },
            {
                "id": "note",
                "kind": "agent_turn",
                "config": {"manifest": "e2e-wf", "prompt": "Note: {{ summary }}"},
            },
        ],
        "edges": [
            _edge("trigger", "issue", "classify", "issue"),
            _edge("classify", "label", "route", "value"),
            _edge("route", "bug", "approve", "when"),
            _edge("classify", "summary", "approve", "payload"),
            _edge("approve", "approved", "fix", "when"),
            _edge("classify", "summary", "fix", "summary"),
            _edge("route", "feature", "note", "when"),
            _edge("classify", "summary", "note", "summary"),
        ],
    }


CLASSIFIED = ScriptedTurn(content='{"label": "bug", "summary": "crash on save"}')


async def _tick(app: Any, run_url: str, until: set[str], *, limit: int = 30) -> dict[str, Any]:
    from felix.durability import fibers as F

    run: dict[str, Any] = {}
    for _ in range(limit):
        for row in F._memory_fibers.values():
            if row.get("status") == "sleeping":
                row["wake_at"] = 0
        await F.resume_due_fibers(app.settings)
        run = (await app.client.get(run_url)).json()
        if run["status"] in until:
            return run
    raise AssertionError(f"run stuck at {run.get('status')}: {run}")


async def _start(app: Any, inputs: dict[str, Any] | None = None) -> str:
    saved = await app.client.put("/workflows/triage", json=_triage())
    assert saved.status_code == 201, saved.text
    started = await app.client.post(
        "/workflows/triage/runs", json={"inputs": inputs or {"issue": "App crashes"}}
    )
    assert started.status_code == 202, started.text
    return f"/workflows/triage/runs/{started.json()['id']}"


async def _decide(app: Any, run: dict[str, Any], decision: str) -> None:
    approval_id = run["steps"]["approve"]["approval_id"]
    decided = await app.client.post(
        f"/approvals/{approval_id}/decide", json={"decision": decision, "note": "ok"}
    )
    assert decided.status_code == 200, decided.text


async def test_a_run_follows_its_branch_waits_on_a_person_and_finishes(boot: Any) -> None:
    script = [CLASSIFIED, ScriptedTurn(content="Patched.")]
    async with boot(script, manifests={"e2e-wf": _manifest()}) as app:
        run_url = await _start(app)
        waiting = await _tick(app, run_url, {"waiting"})
        steps = waiting["steps"]
        assert steps["classify"]["outputs"] == {
            "output": {"label": "bug", "summary": "crash on save"},
            "label": "bug",
            "summary": "crash on save",
        }
        assert steps["route"]["fired"] == ["bug"]
        assert steps["approve"]["status"] == "waiting"
        assert steps["note"]["status"] == "skipped", "the feature path was not taken"
        assert steps["fix"]["status"] == "pending"

        approval = (await app.client.get(f"/approvals/{steps['approve']['approval_id']}")).json()
        assert approval["status"] == "pending"
        assert approval["args"] == {"payload": "crash on save"}
        assert approval["reason"] == "Fix this bug?"

        await _decide(app, waiting, "approved")
        done = await _tick(app, run_url, {"completed", "failed"})
        prompts, options = app.spy.prompts, app.spy.options

    assert done["status"] == "completed", done
    assert done["steps"]["approve"]["fired"] == ["approved"]
    assert done["steps"]["fix"]["outputs"] == {"text": "Patched."}
    assert done["steps"]["fix"]["thread_id"].startswith("default:workflow:")
    # What each agent step was asked: the template filled from the trigger and from classify.
    users = [[str(m.content) for m in p if m.role == "user"][-1] for p in prompts]
    assert users == ["Classify: App crashes", "Fix: crash on save"]
    # The step's own output_schema reached the model call that classified; the fix had none.
    assert options[0] is not None and options[0].output_schema == CLASSIFY_SCHEMA
    assert options[1] is None or getattr(options[1], "output_schema", None) is None


async def test_a_denied_approval_ends_the_run_with_its_path_skipped(boot: Any) -> None:
    async with boot([CLASSIFIED], manifests={"e2e-wf": _manifest()}) as app:
        run_url = await _start(app)
        waiting = await _tick(app, run_url, {"waiting"})
        await _decide(app, waiting, "denied")
        done = await _tick(app, run_url, {"completed", "failed"})
    assert done["status"] == "completed"
    assert done["steps"]["approve"]["fired"] == ["denied"]
    assert done["steps"]["fix"]["status"] == "skipped"


@pytest.mark.parametrize(
    "answer",
    ["It's a bug, I think.", '{"summary": "crash on save"}'],
    ids=["prose", "json-missing-a-required-field"],
)
async def test_an_answer_that_misses_its_output_schema_fails_the_run(boot: Any, answer: str) -> None:
    async with boot([ScriptedTurn(content=answer)], manifests={"e2e-wf": _manifest()}) as app:
        run_url = await _start(app)
        done = await _tick(app, run_url, {"completed", "failed"})
    assert done["status"] == "failed"
    assert done["steps"]["classify"]["status"] == "failed"
    assert "output_schema" in done["steps"]["classify"]["error"]
    assert done["error"].startswith("step classify failed")
    assert {done["steps"][s]["status"] for s in ("route", "approve", "fix", "note")} == {"skipped"}


async def test_cancelling_a_waiting_run_closes_its_approval(boot: Any) -> None:
    async with boot([CLASSIFIED], manifests={"e2e-wf": _manifest()}) as app:
        run_url = await _start(app)
        waiting = await _tick(app, run_url, {"waiting"})
        asked = await app.client.post(f"{run_url}/cancel")
        assert asked.status_code == 200 and asked.json()["status"] == "cancelling"
        done = await _tick(app, run_url, {"cancelled"})
        approval = (await app.client.get(f"/approvals/{waiting['steps']['approve']['approval_id']}")).json()
    assert done["steps"]["approve"]["status"] == "cancelled"
    assert done["steps"]["fix"]["status"] == "cancelled"
    assert approval["status"] == "denied" and approval["decision_note"] == "interrupted", (
        "the card stops asking once nobody is waiting on it"
    )


async def test_editing_the_workflow_mid_run_changes_nothing_the_run_does(boot: Any) -> None:
    async with boot([CLASSIFIED, ScriptedTurn(content="Patched.")], manifests={"e2e-wf": _manifest()}) as app:
        run_url = await _start(app)
        waiting = await _tick(app, run_url, {"waiting"})
        edited = _triage()
        edited["steps"][3]["config"]["prompt"] = "Changed after the run began: {{ summary }}"
        assert (await app.client.put("/workflows/triage", json=edited)).status_code == 200
        await _decide(app, waiting, "approved")
        done = await _tick(app, run_url, {"completed", "failed"})
        listed = (await app.client.get("/workflows/triage/runs")).json()["items"]
        current = (await app.client.get("/workflows/triage")).json()["revision"]
        asked = [[str(m.content) for m in p if m.role == "user"][-1] for p in app.spy.prompts]
    assert done["status"] == "completed"
    assert asked[-1] == "Fix: crash on save", "the step ran as the revision the run started on"
    assert done["revision"] == waiting["revision"] != current
    assert listed[0]["step_statuses"] == {s: done["steps"][s]["status"] for s in done["steps"]}


@pytest.mark.parametrize(
    ("inputs", "detail"),
    [
        ({}, {"error": "bad_inputs", "missing": ["issue"], "unknown": []}),
        ({"issue": "x", "extra": 1}, {"error": "bad_inputs", "missing": [], "unknown": ["extra"]}),
    ],
)
async def test_inputs_must_match_the_workflow(
    boot: Any, inputs: dict[str, Any], detail: dict[str, Any]
) -> None:
    async with boot([], manifests={"e2e-wf": _manifest()}) as app:
        await app.client.put("/workflows/triage", json=_triage())
        refused = await app.client.post("/workflows/triage/runs", json={"inputs": inputs})
    assert refused.status_code == 422
    assert refused.json()["detail"] == detail


async def test_a_workflow_with_problems_or_a_kind_with_no_executor_does_not_start(
    boot: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from felix.workflows import kinds
    from pydantic import BaseModel

    class Wait(BaseModel):
        pass

    # A plugin's kind: registered, so the definition checks, but no executor runs it.
    monkeypatch.setitem(
        kinds._kinds, "wait", kinds.StepKind(config=Wait, ports=lambda _c: ([], []), label="Wait")
    )
    draft = _triage()
    draft["edges"] = draft["edges"][1:]  # classify's `issue` is no longer wired
    plugin = {"name": "Plugin", "steps": [{"id": "pause", "kind": "wait"}]}
    async with boot([], manifests={"e2e-wf": _manifest()}) as app:
        await app.client.put("/workflows/triage", json=draft)
        await app.client.put("/workflows/plugin", json=plugin)
        unwired = await app.client.post("/workflows/triage/runs", json={"inputs": {"issue": "x"}})
        unrunnable = await app.client.post("/workflows/plugin/runs", json={})
        runs = (await app.client.get("/workflows/triage/runs")).json()["items"]
    assert unwired.status_code == 422
    assert [p["code"] for p in unwired.json()["detail"]["problems"]] == ["unconnected_input"]
    assert unrunnable.status_code == 422
    assert unrunnable.json()["detail"]["problems"] == [
        {
            "code": "not_runnable",
            "message": "steps of kind 'wait' cannot run yet",
            "step": "pause",
            "port": None,
        }
    ]
    assert runs == [], "a refused start writes nothing"


async def test_a_manifest_republished_after_the_start_is_not_what_the_step_runs(boot: Any) -> None:
    from felix.manifests.resolver import clear_resolver_cache
    from felix.manifests.store import activate_version, put_version

    async with boot([CLASSIFIED], manifests={"e2e-wf": _manifest()}) as app:
        run_url = await _start(app)
        # Someone holding manifests:write publishes a new version before the first step runs.
        published = await put_version(app.settings, "default", "e2e-wf", _manifest(recursion_limit=3))
        await activate_version(app.settings, "default", "e2e-wf", version=published["version"])
        clear_resolver_cache()
        done = await _tick(app, run_url, {"completed", "failed"})
        calls = list(app.spy.calls)
    assert done["status"] == "failed"
    assert done["steps"]["classify"]["error"] == "the agent run failed"
    assert calls == [], "the drifted manifest never reached the model"


async def test_starting_on_a_stale_revision_is_refused(boot: Any) -> None:
    async with boot([], manifests={"e2e-wf": _manifest()}) as app:
        first = (await app.client.put("/workflows/triage", json=_triage())).json()
        current = (await app.client.put("/workflows/triage", json=_triage())).json()
        stale = await app.client.post(
            "/workflows/triage/runs", json={"inputs": {"issue": "x"}, "revision": first["revision"]}
        )
        fresh = await app.client.post(
            "/workflows/triage/runs", json={"inputs": {"issue": "x"}, "revision": current["revision"]}
        )
    assert stale.status_code == 412
    assert stale.json()["detail"] == {"error": "workflow_changed", "revision": current["revision"]}
    assert fresh.status_code == 202


async def test_runs_are_addressed_under_their_own_workflow(boot: Any) -> None:
    async with boot([CLASSIFIED], manifests={"e2e-wf": _manifest()}) as app:
        run_url = await _start(app)
        run_id = run_url.rsplit("/", 1)[1]
        await app.client.put("/workflows/other", json={"name": "Other"})
        elsewhere = await app.client.get(f"/workflows/other/runs/{run_id}")
        cancel_elsewhere = await app.client.post(f"/workflows/other/runs/{run_id}/cancel")
        missing = await app.client.post("/workflows/nope/runs", json={})
    assert (elsewhere.status_code, cancel_elsewhere.status_code, missing.status_code) == (404, 404, 404)


async def test_cancelling_a_finished_run_changes_nothing(boot: Any) -> None:
    async with boot([CLASSIFIED], manifests={"e2e-wf": _manifest()}) as app:
        run_url = await _start(app)
        waiting = await _tick(app, run_url, {"waiting"})
        await _decide(app, waiting, "denied")
        done = await _tick(app, run_url, {"completed"})
        again = await app.client.post(f"{run_url}/cancel")
    assert again.status_code == 200
    assert (again.json()["status"], again.json()["version"]) == ("completed", done["version"])


async def test_the_run_list_is_newest_first(boot: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from felix.workflows import runs as run_store

    # Runs started in one millisecond tie on `created_at` and then order by id; a moving clock
    # makes "newest" mean the order they were started in.
    clock = iter(range(1_800_000_000_000, 1_800_000_100_000, 10))
    monkeypatch.setattr(run_store, "now_ms", lambda: next(clock))
    async with boot([], manifests={"e2e-wf": _manifest()}) as app:
        await app.client.put("/workflows/triage", json=_triage())
        ids = []
        for _ in range(3):
            started = await app.client.post("/workflows/triage/runs", json={"inputs": {"issue": "x"}})
            ids.append(started.json()["id"])
        listed = [r["id"] for r in (await app.client.get("/workflows/triage/runs")).json()["items"]]
    assert listed == list(reversed(ids))


async def test_run_routes_need_their_scopes(boot: Any) -> None:
    import json

    keys = {
        "reader-token": {"tenant_id": "default", "sub": "ops", "scopes": ["workflows:read"]},
        "writer-token": {"tenant_id": "default", "sub": "ops", "scopes": ["workflows:write"]},
    }
    env = {"FELIX_AUTH_MODE": "api_key", "FELIX_AUTH_API_KEYS": json.dumps(keys)}
    reader, writer = ({"Authorization": f"Bearer {k}"} for k in keys)
    async with boot([], env=env, manifests={"e2e-wf": _manifest()}) as app:
        assert (await app.client.put("/workflows/triage", json=_triage(), headers=writer)).status_code == 201
        body = {"inputs": {"issue": "x"}}
        assert (await app.client.post("/workflows/triage/runs", json=body, headers=reader)).status_code == 403
        started = await app.client.post("/workflows/triage/runs", json=body, headers=writer)
        assert started.status_code == 202, started.text
        run_url = f"/workflows/triage/runs/{started.json()['id']}"
        assert started.json()["started_by"] == "ops"
        assert (await app.client.get(run_url, headers=reader)).status_code == 200
        assert (await app.client.get("/workflows/triage/runs", headers=reader)).status_code == 200
        assert (await app.client.post(f"{run_url}/cancel", headers=reader)).status_code == 403
        assert (await app.client.post(f"{run_url}/cancel", headers=writer)).status_code == 200


# --- tool_call ----------------------------------------------------------------------------

CALC_SCHEMA = {
    "type": "object",
    "properties": {"expression": {"type": "string"}},
    "required": ["expression"],
}


def _calc_manifest(**spec: Any) -> Any:
    return parse_manifest(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": "e2e-calc"},
            "spec": {
                "pattern": "react",
                "tools": ["calculator"],
                "auth": {"inbound": {"allow_anonymous": True}},
                **spec,
            },
        }
    )


def _sums(
    manifest: str = "e2e-calc", schema: dict[str, Any] | None = None, port: str = "expression"
) -> dict[str, Any]:
    """sum → calculator; report → agent told the answer."""
    return {
        "name": "Sums",
        "inputs": {"sum": {"type": "string"}},
        "steps": [
            {
                "id": "calc",
                "kind": "tool_call",
                "config": {"manifest": manifest, "tool": "calculator", "input_schema": schema or CALC_SCHEMA},
            },
            {
                "id": "report",
                "kind": "agent_turn",
                "config": {"manifest": "e2e-wf", "prompt": "The answer is {{ n }}"},
            },
        ],
        "edges": [
            _edge("trigger", "sum", "calc", port),
            _edge("calc", "result", "report", "n"),
        ],
    }


async def _run_sums(app: Any, definition: dict[str, Any]) -> str:
    assert (await app.client.put("/workflows/sums", json=definition)).status_code == 201
    started = await app.client.post("/workflows/sums/runs", json={"inputs": {"sum": "2+2"}})
    assert started.status_code == 202, started.text
    return f"/workflows/sums/runs/{started.json()['id']}"


MANIFESTS = {"e2e-wf": _manifest(), "e2e-calc": _calc_manifest()}


async def test_a_tool_step_calls_the_governed_tool_and_feeds_the_next_step(boot: Any) -> None:
    async with boot([ScriptedTurn(content="Noted.")], manifests=MANIFESTS) as app:
        run_url = await _run_sums(app, _sums())
        done = await _tick(app, run_url, {"completed", "failed"})
        asked = [[str(m.content) for m in p if m.role == "user"][-1] for p in app.spy.prompts]
    assert done["status"] == "completed", done
    assert done["steps"]["calc"]["outputs"] == {"result": 4}
    assert done["steps"]["calc"]["thread_id"].startswith("default:workflow:")
    assert asked == ["The answer is 4"], "the tool's result became the next step's input"


async def test_a_tool_the_manifest_does_not_bind_fails_the_step(boot: Any) -> None:
    async with boot([], manifests=MANIFESTS) as app:
        run_url = await _run_sums(app, _sums(manifest="e2e-wf"))
        done = await _tick(app, run_url, {"completed", "failed"})
    assert done["steps"]["calc"]["status"] == "failed"
    assert done["steps"]["calc"]["error"] == "manifest e2e-wf does not bind tool calculator"


async def test_a_tool_whose_schema_drifted_since_wiring_fails_the_step(boot: Any) -> None:
    # Wired against an older calculator that took `expr`; the live one requires `expression`.
    old = {"type": "object", "properties": {"expr": {"type": "string"}}, "required": ["expr"]}
    async with boot([], manifests=MANIFESTS) as app:
        run_url = await _run_sums(app, _sums(schema=old, port="expr"))
        done = await _tick(app, run_url, {"completed", "failed"})
    assert done["steps"]["calc"]["status"] == "failed"
    assert done["steps"]["calc"]["error"].startswith("the tool now requires expression")


async def test_a_tool_step_is_governed_by_its_manifests_approvals(boot: Any) -> None:
    import asyncio

    from felix.durability import fibers as F

    gated = {
        "e2e-wf": _manifest(),
        "e2e-calc": _calc_manifest(approvals=[{"id": "ask", "tools": ["calculator"]}]),
    }
    async with boot([ScriptedTurn(content="Noted.")], manifests=gated) as app:
        run_url = await _run_sums(app, _sums())
        await F.resume_due_fibers(app.settings)  # the orchestrator starts the tool step's fiber
        sweep = asyncio.create_task(F.resume_due_fibers(app.settings))  # the call waits on its gate
        pending: list[dict[str, Any]] = []
        for _ in range(100):
            pending = (await app.client.get("/approvals")).json()["items"]
            if pending:
                break
            await asyncio.sleep(0.05)
        assert [p["tool_name"] for p in pending] == ["calculator"], "the manifest's approval rule applied"
        await app.client.post(f"/approvals/{pending[0]['id']}/decide", json={"decision": "approved"})
        await sweep
        done = await _tick(app, run_url, {"completed", "failed"})
    assert done["status"] == "completed", done
    assert done["steps"]["calc"]["outputs"] == {"result": 4}


async def test_a_tool_step_refuses_a_manifest_republished_after_the_start(boot: Any) -> None:
    from felix.manifests.resolver import clear_resolver_cache
    from felix.manifests.store import activate_version, put_version

    async with boot([], manifests=MANIFESTS) as app:
        run_url = await _run_sums(app, _sums())
        published = await put_version(app.settings, "default", "e2e-calc", _calc_manifest(recursion_limit=3))
        await activate_version(app.settings, "default", "e2e-calc", version=published["version"])
        clear_resolver_cache()
        done = await _tick(app, run_url, {"completed", "failed"})
    assert done["steps"]["calc"]["status"] == "failed"
    assert done["steps"]["calc"]["error"] == "manifest e2e-calc changed since the run started"
