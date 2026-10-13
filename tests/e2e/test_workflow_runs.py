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


async def _tick(
    app: Any, run_url: str, until: set[str], *, limit: int = 30, headers: dict[str, str] | None = None
) -> dict[str, Any]:
    from felix.durability import fibers as F

    run: dict[str, Any] = {}
    for _ in range(limit):
        for row in F._memory_fibers.values():
            if row.get("status") == "sleeping":
                row["wake_at"] = 0
        await F.resume_due_fibers(app.settings)
        run = (await app.client.get(run_url, headers=headers)).json()
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


# --- the editor's tool listing ----------------------------------------------------------------

PICK = {"type": "object", "properties": {"choice": {"type": "string"}}, "required": ["choice"]}


async def test_a_step_wired_from_the_tool_listing_runs(boot: Any) -> None:
    calc = _calc_manifest(client_tools=[{"name": "pick_colour", "args_schema": PICK}])
    async with boot([ScriptedTurn(content="Noted.")], manifests={**MANIFESTS, "e2e-calc": calc}) as app:
        listed = await app.client.get("/workflows/-/tools", params={"manifest": "e2e-calc"})
        assert listed.status_code == 200, listed.text
        tools = {t["name"]: t for t in listed.json()["items"]}
        assert "pick_colour" not in tools, "a client tool has no client to answer it in a workflow"
        calculator = tools["calculator"]
        assert calculator["problem"] is None
        assert [(p["name"], p["type"], p["required"]) for p in calculator["inputs"]] == [
            ("expression", "string", True)
        ]
        # Carried through every governance wrapper's clone, not defaulted on the way.
        assert (calculator["read_only"], calculator["replay_safe"]) == (True, True)
        # The step stores what the listing gave it, and the run's drift check accepts it.
        run_url = await _run_sums(app, _sums(schema=calculator["input_schema"]))
        done = await _tick(app, run_url, {"completed", "failed"})
    assert done["status"] == "completed", done
    assert done["steps"]["calc"]["outputs"] == {"result": 4}


async def test_a_step_naming_a_client_tool_fails_without_waiting_on_it(boot: Any) -> None:
    calc = _calc_manifest(client_tools=[{"name": "pick_colour", "args_schema": PICK}])
    definition = {
        "name": "Pick",
        "steps": [
            {
                "id": "pick",
                "kind": "tool_call",
                "config": {
                    "manifest": "e2e-calc",
                    "tool": "pick_colour",
                    "input_schema": PICK,
                    "args": {"choice": "red"},
                },
            }
        ],
    }
    async with boot([], manifests={**MANIFESTS, "e2e-calc": calc}) as app:
        assert (await app.client.put("/workflows/pick", json=definition)).status_code == 201
        started = await app.client.post("/workflows/pick/runs", json={"inputs": {}})
        assert started.status_code == 202, started.text
        done = await _tick(app, f"/workflows/pick/runs/{started.json()['id']}", {"completed", "failed"})
    assert done["steps"]["pick"]["status"] == "failed"
    assert done["steps"]["pick"]["error"] == (
        "tool pick_colour runs in the chat client, which a workflow does not have"
    )


async def test_the_tool_listing_answers_for_the_manifest_as_the_caller_may_use_it(boot: Any) -> None:
    import json

    keys = {
        "reader-token": {"tenant_id": "default", "sub": "ops", "scopes": ["workflows:read"]},
        "other-token": {"tenant_id": "default", "sub": "ops", "scopes": ["jobs:read"]},
    }
    env = {"FELIX_AUTH_MODE": "api_key", "FELIX_AUTH_API_KEYS": json.dumps(keys)}
    reader, other = ({"Authorization": f"Bearer {k}"} for k in keys)
    guarded = _calc_manifest(auth={"inbound": {"required_scopes": ["calc:use"]}})
    async with boot([], env=env, manifests={**MANIFESTS, "e2e-guarded": guarded}) as app:

        async def listing(manifest: str, headers: dict[str, str]) -> Any:
            return await app.client.get("/workflows/-/tools", params={"manifest": manifest}, headers=headers)

        assert (await listing("e2e-calc", other)).status_code == 403, "needs workflows:read"
        assert (await listing("e2e-calc", reader)).status_code == 200
        refused = await listing("e2e-guarded", reader)
        assert refused.status_code == 403
        assert refused.json()["detail"] == {"error": "not_permitted", "manifest": "e2e-guarded"}
        missing = await listing("no-such-manifest", reader)
        assert missing.status_code == 404
        assert missing.json()["detail"] == {"error": "unknown_manifest", "manifest": "no-such-manifest"}
        malformed = await listing("Bad Name", reader)
        assert malformed.status_code == 404, "a name no manifest can have is not found, not unavailable"


async def test_the_tool_listing_names_a_manifest_that_refuses_anonymous_or_does_not_compile(
    boot: Any,
) -> None:
    closed = _calc_manifest(auth={"inbound": {"allow_anonymous": False}})
    broken = _calc_manifest(memory={"checkpointer": "no-such-checkpointer"})
    manifests = {**MANIFESTS, "e2e-closed": closed, "e2e-broken": broken}
    async with boot([], manifests=manifests) as app:
        refused = await app.client.get("/workflows/-/tools", params={"manifest": "e2e-closed"})
        assert refused.status_code == 401, "an anonymous caller is told to authenticate, not forbidden"
        assert refused.json()["detail"] == {"error": "not_permitted", "manifest": "e2e-closed"}
        unavailable = await app.client.get("/workflows/-/tools", params={"manifest": "e2e-broken"})
        assert unavailable.status_code == 503
        assert unavailable.json()["detail"] == {"error": "manifest_unavailable", "manifest": "e2e-broken"}
        assert "checkpointer" not in unavailable.text, "the compile's own error stays in the log"


# --- a job that starts a workflow run ---------------------------------------------------------

JOB_KEYS = {
    "ops-token": {
        "tenant_id": "default",
        "sub": "ops",
        "scopes": ["jobs:write", "workflows:write", "calc:use"],
    },
    "jobs-token": {"tenant_id": "default", "sub": "sched", "scopes": ["jobs:write", "workflows:read"]},
    "root-token": {"tenant_id": "default", "sub": "root", "scopes": ["admin", "calc:admin"]},
}


def _job_env() -> dict[str, str]:
    import json

    return {"FELIX_AUTH_MODE": "api_key", "FELIX_AUTH_API_KEYS": json.dumps(JOB_KEYS)}


OPS, JOBS_ONLY, ROOT = ({"Authorization": f"Bearer {k}"} for k in JOB_KEYS)

# A tool step whose manifest admits only `calc:use` (ops holds it) or only `calc:admin` (root).
JOB_MANIFESTS = {
    **MANIFESTS,
    "e2e-calc": _calc_manifest(auth={"inbound": {"required_scopes": ["calc:use"]}}),
    "e2e-root": _calc_manifest(auth={"inbound": {"required_scopes": ["calc:admin"]}}),
}
NIGHTLY = {"schedule": "@daily", "workflow_id": "sums", "payload": {"inputs": {"sum": "2+2"}}}


async def _job_on_sums(app: Any, definition: dict[str, Any] | None = None) -> None:
    saved = await app.client.put("/workflows/sums", json=definition or _sums(), headers=OPS)
    assert saved.status_code == 201, saved.text
    written = await app.client.put("/jobs/nightly", json=NIGHTLY, headers=OPS)
    assert written.status_code == 200, written.text


async def _only_run(app: Any) -> dict[str, Any]:
    from felix.jobs import store as jobs_store

    (record,) = await jobs_store.list_runs(app.settings, "default", "nightly")
    return record


async def test_a_scheduled_firing_runs_the_workflow_as_the_jobs_writer(boot: Any) -> None:
    """No request in context: what the run records and its steps run with is the writer's alone."""
    from felix.audit import store as audit_store
    from felix.jobs.scheduler import run_due_jobs

    async with boot([ScriptedTurn(content="Noted.")], env=_job_env(), manifests=JOB_MANIFESTS) as app:
        await _job_on_sums(app)
        assert await run_due_jobs(app.settings) == 1, "a job never run is due"
        record = await _only_run(app)
        assert record["status"] == "ok", record
        run_url = f"/workflows/sums/runs/{record['result']['workflow_run_id']}"
        started = (await app.client.get(run_url, headers=OPS)).json()
        assert (started["trigger"], started["started_by"]) == ("job:nightly", "ops")
        assert started["inputs"] == {"sum": "2+2"}
        # The calc step's manifest needs `calc:use`: it runs only on the writer's recorded scopes.
        done = await _tick(app, run_url, {"completed", "failed"}, headers=OPS)
        await audit_store.flush_pending(app.settings)
        events, _ = await audit_store.list_events(app.settings, "default", event_type="workflow_run_started")
    assert done["status"] == "completed", done
    assert done["steps"]["calc"]["outputs"] == {"result": 4}
    assert [(e["principal_subj"], e["payload_json"]["trigger"]) for e in events] == [("ops", "job:nightly")]


async def test_an_operator_running_someone_elses_job_lends_it_nothing(boot: Any) -> None:
    async with boot([ScriptedTurn(content="Noted.")], env=_job_env(), manifests=JOB_MANIFESTS) as app:
        await _job_on_sums(app, _sums(manifest="e2e-root"))
        fired = await app.client.post("/jobs/nightly/run", headers=ROOT)
        assert fired.status_code == 200, fired.text
        listed = (await app.client.get("/workflows/sums/runs", headers=OPS)).json()["items"]
    # Root may run the job by hand, and the run is still ops's: `calc:admin` is root's, not ops's.
    assert (fired.json()["status"], fired.json()["error"]) == (
        "error",
        "workflow sums: the starter may not use manifest e2e-root",
    )
    assert listed == [], "nothing was started"


async def test_a_job_refuses_a_workflow_saved_again_since_it_was_written(boot: Any) -> None:
    async with boot([ScriptedTurn(content="Noted.")], env=_job_env(), manifests=JOB_MANIFESTS) as app:
        await _job_on_sums(app)
        # Someone else with `workflows:write` saves a new definition: the job does not run it
        # with ops's scopes until ops, having seen it, re-saves the job.
        assert (await app.client.put("/workflows/sums", json=_sums(), headers=ROOT)).status_code == 200
        refused = (await app.client.post("/jobs/nightly/run", headers=OPS)).json()
        assert (refused["status"], refused["error"]) == (
            "error",
            "workflow sums changed since this job was written; re-save the job to run it",
        )
        assert (await app.client.get("/workflows/sums/runs", headers=OPS)).json()["items"] == []
        assert (await app.client.put("/jobs/nightly", json=NIGHTLY, headers=OPS)).status_code == 200
        again = (await app.client.post("/jobs/nightly/run", headers=OPS)).json()
    assert again["status"] == "ok", again


async def test_writing_a_workflow_job_checks_the_grant_the_workflow_and_its_inputs(boot: Any) -> None:
    async with boot([], env=_job_env(), manifests=JOB_MANIFESTS) as app:
        assert (await app.client.put("/workflows/sums", json=_sums(), headers=OPS)).status_code == 201

        async def put(job: dict[str, Any], headers: dict[str, str] = OPS) -> Any:
            return await app.client.put("/jobs/a", json={**NIGHTLY, **job}, headers=headers)

        assert (await put({}, JOBS_ONLY)).status_code == 403
        both = await put({"manifest_id": "e2e-wf"})
        assert (both.status_code, both.json()["detail"]) == (422, {"error": "manifest_and_workflow"})
        missing = await put({"workflow_id": "nope"})
        assert (missing.status_code, missing.json()["detail"]) == (422, {"error": "unknown_workflow"})
        misnamed = await put({"payload": {"inputs": {"total": "1"}}})
        assert (misnamed.status_code, misnamed.json()["detail"]) == (
            422,
            {"error": "bad_inputs", "missing": ["sum"], "unknown": ["total"]},
        )
        listed = await put({"payload": {"inputs": ["1"]}})
        assert (listed.status_code, listed.json()["detail"]["error"]) == (422, "bad_inputs")
        assert (await app.client.get("/jobs/a", headers=OPS)).status_code == 404, "nothing was written"
        current = (await app.client.get("/workflows/sums", headers=OPS)).json()["revision"]
        stale = await put({"workflow_revision": "0.0"})
        assert (stale.status_code, stale.json()["detail"]) == (
            412,
            {"error": "workflow_changed", "revision": current},
        ), "an edit saved while the writer read is not what the job pins"
        assert (await app.client.get("/jobs/a", headers=OPS)).status_code == 404
        written = await put({"workflow_revision": current})
        assert written.status_code == 200, written.text
        assert written.json()["workflow_revision"] == current


OPS_AUTH = {"scopes": ["jobs:write", "workflows:write", "calc:use"], "scheme": "access"}


@pytest.mark.parametrize(
    ("job", "error"),
    [
        (
            {"payload": {"inputs": {"total": "1"}}},
            "workflow sums: the inputs do not match the workflow: missing sum; unknown total",
        ),
        ({"payload": {"inputs": ["1"]}}, "payload.inputs must be an object of the workflow's inputs"),
        ({"workflow_id": "gone"}, "workflow gone does not exist"),
        (
            {"created_auth": {"scopes": ["jobs:write"], "scheme": "access"}},
            "the job's writer may not start workflow runs (workflows:write)",
        ),
        # An API-key writer is held to what the key holds now: `jobs-token` lacks workflows:write.
        (
            {"created_by": "sched", "created_auth": {**OPS_AUTH, "scheme": "api_key"}},
            "the job's writer may not start workflow runs (workflows:write)",
        ),
        (
            {"created_by": "ghost", "created_auth": {**OPS_AUTH, "scheme": "api_key"}},
            "the API key this job's writer used is no longer configured; re-save the job as someone "
            "who may run it",
        ),
        (
            {"created_auth": {**OPS_AUTH, "scopes": ["jobs:write", "workflows:write"]}},
            ("workflow sums: the starter may not use manifest e2e-calc"),
        ),
        (
            {"workflow_revision": "0.0"},
            "workflow sums changed since this job was written; re-save the job to run it",
        ),
    ],
)
async def test_a_firing_that_cannot_start_the_run_says_why(
    boot: Any, job: dict[str, Any], error: str
) -> None:
    from felix.jobs import store as jobs_store
    from felix.jobs.scheduler import fire_job, now_ms

    async with boot([], env=_job_env(), manifests=JOB_MANIFESTS) as app:
        saved = await app.client.put("/workflows/sums", json=_sums(), headers=OPS)
        assert saved.status_code == 201
        # Written to the store directly, and fired with no request in context: the route refuses
        # some of these, and a stored job drifts into each anyway -- the workflow deleted or
        # edited, its inputs renamed, the writer's key cut down or removed.
        fields = {
            "workflow_id": "sums",
            "workflow_revision": saved.json()["revision"],
            "payload": {"inputs": {"sum": "1"}},
            "created_by": "ops",
            "created_auth": OPS_AUTH,
            **job,
        }
        stored = await jobs_store.put_job(app.settings, "default", "nightly", **fields)
        run = await fire_job(app.settings, "default", stored, started_at=now_ms())
        listed = (await app.client.get("/workflows/sums/runs", headers=OPS)).json()["items"]
    assert (run["status"], run["error"]) == ("error", error)
    assert listed == [], "nothing was started"
