"""The workflow engine's promises that an end-to-end run cannot easily break on purpose.

Each drives `engine.advance` directly against the memory stores: a start replayed after its save
was lost starts nothing twice, a child run is started as the workflow's starter and dies with the
run, and a run past its deadline stops what it started.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.context import AuthContext, RequestContext, async_run_with_context
from felix.durability import fibers as F
from felix.workflows import engine
from felix.workflows import runs as run_store
from felix.workflows import store as workflow_store
from felix.workflows.schema import WorkflowSpec

from tests.support.factories import make_settings

TENANT = "default"


def _agent_then_gate() -> dict[str, Any]:
    return {
        "name": "Two steps",
        "inputs": {"topic": {"type": "string"}},
        "steps": [
            {
                "id": "write",
                "kind": "agent_turn",
                "config": {"manifest": "quick", "prompt": "On {{ topic }}"},
            },
            {"id": "gate", "kind": "approval", "config": {}},
        ],
        "edges": [
            {"source": {"step": "trigger", "port": "topic"}, "target": {"step": "write", "port": "topic"}}
        ],
    }


async def _start(settings: Any, *, scopes: frozenset[str] = frozenset()) -> dict[str, Any]:
    workflow = await workflow_store.put_workflow(
        settings, TENANT, "two", WorkflowSpec.model_validate(_agent_then_gate())
    )
    caller = RequestContext(
        settings=settings,
        auth=AuthContext(
            principal_sub="alice", tenant_id=TENANT, scopes=scopes, anonymous=False, scheme="api_key"
        ),
    )
    async with async_run_with_context(caller):
        return await engine.start_run(
            settings, TENANT, workflow, {"topic": "tides"}, trigger="manual", started_by="alice"
        )


def _children(run_id: str) -> list[dict[str, Any]]:
    return [
        row
        for (tenant, _), row in F._memory_fibers.items()
        if tenant == TENANT and str(row.get("thread_id") or "").startswith(f"{TENANT}:workflow:{run_id}:")
    ]


async def test_a_start_replayed_after_a_lost_save_starts_nothing_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from felix.approvals import store as approvals

    settings = make_settings()
    run = await _start(settings)
    real_save = run_store.save_run
    lost = {"n": 0}

    async def lose_first(settings: Any, row: dict[str, Any]) -> bool:
        # The first look's save is lost after its side effects happened, as a crashed worker's is.
        if lost["n"] == 0 and row["id"] == run["id"]:
            lost["n"] += 1
            return False
        return await real_save(settings, row)

    monkeypatch.setattr(run_store, "save_run", lose_first)
    first = await engine.advance(settings, TENANT, run["id"])
    assert first.finished is False and first.delay_ms == 0, "a lost save looks again at once"
    assert await run_store.get_run(settings, TENANT, run["id"]) is not None
    await engine.advance(settings, TENANT, run["id"])

    after = await run_store.get_run(settings, TENANT, run["id"])
    assert after is not None
    assert len(_children(run["id"])) == 1, "one child run for the agent step, not one per look"
    assert after["steps"]["write"]["status"] == "running"
    gates = await approvals.list_approvals(settings, TENANT, status="pending")
    assert len([g for g in gates if g["call_signature"] == f"{run['id']}:gate"]) == 1
    assert after["steps"]["gate"]["status"] == "waiting"


async def test_a_child_run_starts_as_the_starter_and_dies_with_the_run() -> None:
    settings = make_settings()
    run = await _start(settings, scopes=frozenset({"workflows:write", "memory:read"}))
    await engine.advance(settings, TENANT, run["id"])
    (child,) = _children(run["id"])
    state = child["state_json"]
    assert state["auth"]["principal_sub"] == "alice", "the person, never the worker"
    assert state["auth"]["scopes"] == ["memory:read", "workflows:write"]
    assert state["expires_at"] <= run["expires_at"], "a child's authority cannot outlive the run's"
    assert state["steps"][0]["messages"][0]["content"] == "On tides"


async def test_an_agent_step_resumes_as_a_machine_acting_for_the_starter() -> None:
    """What the worker rebuilds from the child's state (`auth_from_state`, the one reading every
    resume uses): the actor is the fiber, and `acting_for` -- what an approval names as its
    requester and what `publish_commits` matches against a checkout's opener -- is the person
    who started the workflow."""
    settings = make_settings()
    run = await _start(settings, scopes=frozenset({"workflows:write"}))
    await engine.advance(settings, TENANT, run["id"])
    (child,) = _children(run["id"])

    resumed = F.auth_from_state(TENANT, child["state_json"]["auth"])
    assert (resumed.principal_sub, resumed.acting_for) == ("fiber", "alice")
    assert resumed.scopes == frozenset({"workflows:write"})
    assert resumed.scheme == "api_key"


async def test_a_run_with_no_recorded_caller_starts_children_with_no_authority() -> None:
    settings = make_settings()
    workflow = await workflow_store.put_workflow(
        settings, TENANT, "two", WorkflowSpec.model_validate(_agent_then_gate())
    )
    run = await engine.start_run(
        settings, TENANT, workflow, {"topic": "tides"}, trigger="manual", started_by=""
    )
    await engine.advance(settings, TENANT, run["id"])
    (child,) = _children(run["id"])
    assert "auth" not in child["state_json"], "fail closed: no caller, no scopes on resume"


async def test_a_run_past_its_deadline_stops_what_it_started() -> None:
    settings = make_settings()
    run = await _start(settings)
    await engine.advance(settings, TENANT, run["id"])
    run_store._memory_runs[(TENANT, run["id"])]["expires_at"] = 0
    outcome = await engine.advance(settings, TENANT, run["id"])
    after = await run_store.get_run(settings, TENANT, run["id"])
    assert outcome.finished is True and outcome.error == "the run passed its deadline"
    assert after is not None and after["status"] == "expired"
    assert {s["status"] for s in after["steps"].values()} == {"cancelled"}


async def test_a_terminal_run_is_never_advanced_again() -> None:
    settings = make_settings()
    run = await _start(settings)
    await engine.request_cancel(settings, TENANT, run["id"])
    first = await engine.advance(settings, TENANT, run["id"])
    assert first.finished is True and first.error == "", "a cancelled run is not a failure"
    version = (await run_store.get_run(settings, TENANT, run["id"]))["version"]  # type: ignore[index]
    again = await engine.advance(settings, TENANT, run["id"])
    assert again.finished is True
    assert (await run_store.get_run(settings, TENANT, run["id"]))["version"] == version  # type: ignore[index]
    assert _children(run["id"]) == [], "cancelled before its first look: nothing was started"


async def test_the_orchestrator_fiber_finishes_with_the_run() -> None:
    settings = make_settings()
    run = await _start(settings)
    await engine.request_cancel(settings, TENANT, run["id"])
    fiber = F._memory_fibers[(TENANT, run["fiber_id"])]
    assert fiber["kind"] == engine.ORCHESTRATOR_KIND
    await F.resume_due_fibers(settings)
    assert F._memory_fibers[(TENANT, run["fiber_id"])]["status"] == "completed"


async def test_a_failed_step_aborts_the_sibling_still_running() -> None:
    from felix.steer import is_aborted

    settings = make_settings()
    parallel = {
        "name": "Two at once",
        "steps": [
            {"id": "left", "kind": "agent_turn", "config": {"manifest": "quick", "prompt": "Left"}},
            {"id": "right", "kind": "agent_turn", "config": {"manifest": "quick", "prompt": "Right"}},
        ],
    }
    workflow = await workflow_store.put_workflow(
        settings, TENANT, "both", WorkflowSpec.model_validate(parallel)
    )
    run = await engine.start_run(settings, TENANT, workflow, {}, trigger="manual", started_by="")
    await engine.advance(settings, TENANT, run["id"])
    started = await run_store.get_run(settings, TENANT, run["id"])
    assert started is not None
    assert {s["status"] for s in started["steps"].values()} == {"running"}

    # The left child fails; the right one is still mid-turn.
    left = F._memory_fibers[(TENANT, started["steps"]["left"]["fiber_id"])]
    left["status"] = "failed"
    left["state_json"]["stash"] = {
        "last": {"answer": "", "final": "", "error": "model refused", "manifest_id": "quick"}
    }
    outcome = await engine.advance(settings, TENANT, run["id"])
    after = await run_store.get_run(settings, TENANT, run["id"])

    assert outcome.finished is True
    assert after is not None and after["status"] == "failed"
    assert after["error"] == "step left failed: the agent run failed"
    assert "model refused" not in str(after), "the child's own error text is logged, never returned"
    assert after["steps"]["right"]["status"] == "cancelled"
    assert await is_aborted(TENANT, after["steps"]["right"]["thread_id"]), (
        "the sibling's run was told to stop"
    )


async def test_an_approval_nobody_decides_fails_rather_than_denies() -> None:
    from felix.approvals import store as approvals

    settings = make_settings()
    gate_only = {"name": "Gate", "steps": [{"id": "gate", "kind": "approval", "config": {"ttl_seconds": 60}}]}
    workflow = await workflow_store.put_workflow(
        settings, TENANT, "gate", WorkflowSpec.model_validate(gate_only)
    )
    run = await engine.start_run(settings, TENANT, workflow, {}, trigger="manual", started_by="alice")
    await engine.advance(settings, TENANT, run["id"])
    waiting = await run_store.get_run(settings, TENANT, run["id"])
    assert waiting is not None and waiting["status"] == "waiting"
    approvals._memory_approvals[(TENANT, waiting["steps"]["gate"]["approval_id"])]["expires_at"] = 1

    await engine.advance(settings, TENANT, run["id"])
    after = await run_store.get_run(settings, TENANT, run["id"])
    assert after is not None and after["status"] == "failed"
    assert after["steps"]["gate"]["status"] == "failed"
    assert after["steps"]["gate"]["error"] == "no decision before the approval expired"
    assert "fired" not in after["steps"]["gate"], "a timeout fires neither port"


# --- review follow-ups ---------------------------------------------------------------------


async def _workflow(settings: Any, workflow_id: str, definition: dict[str, Any]) -> dict[str, Any]:
    return await workflow_store.put_workflow(
        settings, TENANT, workflow_id, WorkflowSpec.model_validate(definition)
    )


async def test_a_sweeper_timeout_fails_the_step_rather_than_denying() -> None:
    from felix.approvals import store as approvals

    settings = make_settings()
    workflow = await _workflow(
        settings, "gate", {"name": "Gate", "steps": [{"id": "gate", "kind": "approval"}]}
    )
    run = await engine.start_run(settings, TENANT, workflow, {}, trigger="manual", started_by="alice")
    await engine.advance(settings, TENANT, run["id"])
    waiting = await run_store.get_run(settings, TENANT, run["id"])
    assert waiting is not None
    row = approvals._memory_approvals[(TENANT, waiting["steps"]["gate"]["approval_id"])]
    row.update(status="denied", decided_by=approvals.TIMEOUT_DECIDER, decision_note=approvals.TIMEOUT_NOTE)
    await engine.advance(settings, TENANT, run["id"])
    after = await run_store.get_run(settings, TENANT, run["id"])
    assert after is not None and after["steps"]["gate"]["status"] == "failed"


async def test_cancelling_stops_a_child_that_was_never_claimed() -> None:
    settings = make_settings()
    run = await _start(settings)
    await engine.advance(settings, TENANT, run["id"])
    (child,) = _children(run["id"])
    assert child["status"] == "pending", "enqueued, no worker has claimed it"
    await engine.request_cancel(settings, TENANT, run["id"])
    await engine.advance(settings, TENANT, run["id"])
    assert F._memory_fibers[(TENANT, child["id"])]["status"] == "failed", "it will never run"


async def test_a_child_started_by_a_lost_attempt_is_stopped_although_its_step_reads_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = make_settings()
    run = await _start(settings)
    real_save = run_store.save_run

    async def lose(settings: Any, row: dict[str, Any]) -> bool:
        return False

    monkeypatch.setattr(run_store, "save_run", lose)
    await engine.advance(settings, TENANT, run["id"])  # starts the child, loses the save
    monkeypatch.setattr(run_store, "save_run", real_save)
    stored = await run_store.get_run(settings, TENANT, run["id"])
    assert stored is not None and stored["steps"]["write"]["status"] == "pending"
    (child,) = _children(run["id"])

    await engine.request_cancel(settings, TENANT, run["id"])
    await engine.advance(settings, TENANT, run["id"])
    assert F._memory_fibers[(TENANT, child["id"])]["status"] == "failed"


async def test_a_buried_orchestrator_fails_its_run() -> None:
    settings = make_settings()
    run = await _start(settings)
    await engine.advance(settings, TENANT, run["id"])
    fiber = dict(F._memory_fibers[(TENANT, run["fiber_id"])], status="dead")
    await engine.orchestrator_ended(settings, fiber)
    after = await run_store.get_run(settings, TENANT, run["id"])
    assert after is not None and after["status"] == "failed"
    assert after["error"] == "the run stopped: its orchestrator failed"
    assert after["steps"]["write"]["status"] == "cancelled"


async def test_a_second_claim_adopts_the_child_the_first_just_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = make_settings()
    run = await _start(settings)
    await engine.advance(settings, TENANT, run["id"])
    (child,) = _children(run["id"])
    # As if this claim read the run before the other claim's child existed.
    run_store._memory_runs[(TENANT, run["id"])]["steps"]["write"] = {"status": "pending"}

    async def not_yet(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(F, "get_fiber", not_yet)
    await engine.advance(settings, TENANT, run["id"])
    after = await run_store.get_run(settings, TENANT, run["id"])
    assert after is not None and after["steps"]["write"]["status"] == "running"
    assert after["steps"]["write"]["fiber_id"] == child["id"]


async def test_at_most_eight_agent_steps_of_a_run_are_in_flight() -> None:
    settings = make_settings()
    wide = {
        "name": "Wide",
        "steps": [
            {"id": f"s{i}", "kind": "agent_turn", "config": {"manifest": "quick", "prompt": "Go"}}
            for i in range(10)
        ],
    }
    run = await engine.start_run(
        settings, TENANT, await _workflow(settings, "wide", wide), {}, trigger="manual", started_by=""
    )
    await engine.advance(settings, TENANT, run["id"])
    after = await run_store.get_run(settings, TENANT, run["id"])
    assert after is not None
    statuses = [s["status"] for s in after["steps"].values()]
    assert statuses.count("running") == engine.MAX_AGENT_STEPS_IN_FLIGHT
    assert statuses.count("pending") == 2


async def test_a_tenant_at_its_run_limit_cannot_start_another(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = make_settings()
    monkeypatch.setattr(engine, "MAX_ACTIVE_RUNS_PER_TENANT", 1)
    await _start(settings)
    with pytest.raises(engine.TooManyRuns):
        await _start(settings)


async def test_a_manifest_that_refuses_the_starter_refuses_the_run() -> None:
    from felix.manifests.loader import parse_manifest
    from felix.manifests.store import put_version

    settings = make_settings()
    guarded = parse_manifest(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": "guarded"},
            "spec": {"pattern": "react", "auth": {"inbound": {"required_scopes": ["ops:run"]}}},
        }
    )
    await put_version(settings, TENANT, "guarded", guarded)
    definition = {
        "name": "Guarded",
        "steps": [{"id": "go", "kind": "agent_turn", "config": {"manifest": "guarded", "prompt": "Go"}}],
    }
    workflow = await _workflow(settings, "guarded", definition)
    caller = RequestContext(
        settings=settings,
        auth=AuthContext(
            principal_sub="bob", tenant_id=TENANT, scopes=frozenset(), anonymous=False, scheme="api_key"
        ),
    )
    async with async_run_with_context(caller):
        with pytest.raises(engine.NotPermitted) as refused:
            await engine.start_run(settings, TENANT, workflow, {}, trigger="manual", started_by="bob")
    assert refused.value.manifest == "guarded"
    assert await run_store.list_runs(settings, TENANT, "guarded") == []


async def test_each_manifest_is_pinned_at_start_and_the_child_carries_it() -> None:
    settings = make_settings()
    run = await _start(settings)
    stored = await run_store.get_run(settings, TENANT, run["id"])
    assert stored is not None and set(stored["pins"]) == {"quick"}
    assert stored["pins"]["quick"]["pin"]["manifest_hash"]
    await engine.advance(settings, TENANT, run["id"])
    (child,) = _children(run["id"])
    assert child["state_json"]["pin"] == stored["pins"]["quick"]["pin"]


async def test_a_look_that_changes_nothing_writes_nothing() -> None:
    settings = make_settings()
    run = await _start(settings)
    await engine.advance(settings, TENANT, run["id"])
    version = (await run_store.get_run(settings, TENANT, run["id"]))["version"]  # type: ignore[index]
    outcome = await engine.advance(settings, TENANT, run["id"])
    assert outcome.finished is False and outcome.delay_ms == engine.POLL_MS
    assert (await run_store.get_run(settings, TENANT, run["id"]))["version"] == version  # type: ignore[index]


async def test_the_orchestrator_parks_between_looks_and_fails_with_a_failed_run() -> None:
    settings = make_settings()
    run = await _start(settings)
    before = F.now_ms()
    await F.resume_due_fibers(settings)
    fiber = F._memory_fibers[(TENANT, run["fiber_id"])]
    assert fiber["status"] == "sleeping"
    assert before + engine.POLL_MS <= fiber["wake_at"] <= F.now_ms() + engine.POLL_MS

    run_store._memory_runs[(TENANT, run["id"])]["expires_at"] = 0
    fiber["wake_at"] = 0
    await F.resume_due_fibers(settings)
    assert F._memory_fibers[(TENANT, run["fiber_id"])]["status"] == "failed", (
        "an expired run is not a success"
    )


@pytest.mark.parametrize(
    ("cases", "value", "fired", "error"),
    [
        ([], True, ["true"], None),
        ([], False, ["false"], None),
        ([], "yes", None, "expected a boolean, got str"),
        (["bug", "feature"], "chore", ["default"], None),
    ],
)
async def test_branch_routes_booleans_and_unknown_cases(
    cases: list[str], value: Any, fired: list[str] | None, error: str | None
) -> None:
    settings = make_settings()
    definition = {
        "name": "Route",
        # Untyped, so a wrong-typed value reaches the branch and the branch itself refuses it.
        "inputs": {"value": {}},
        "steps": [{"id": "route", "kind": "branch", "config": {"cases": cases}}],
        "edges": [
            {"source": {"step": "trigger", "port": "value"}, "target": {"step": "route", "port": "value"}}
        ],
    }
    workflow = await _workflow(settings, "route", definition)
    run = await engine.start_run(
        settings, TENANT, workflow, {"value": value}, trigger="manual", started_by=""
    )
    await engine.advance(settings, TENANT, run["id"])
    step = (await run_store.get_run(settings, TENANT, run["id"]))["steps"]["route"]  # type: ignore[index]
    assert step.get("fired") == fired
    assert step.get("error") == error


async def test_a_step_reading_a_port_of_a_skipped_step_is_skipped() -> None:
    settings = make_settings()
    definition = {
        "name": "Skip",
        "inputs": {"flag": {"type": "boolean"}},
        "steps": [
            {"id": "route", "kind": "branch", "config": {}},
            {"id": "gate", "kind": "approval", "config": {}},
            {"id": "after", "kind": "approval", "config": {}},
        ],
        "edges": [
            {"source": {"step": "trigger", "port": "flag"}, "target": {"step": "route", "port": "value"}},
            {"source": {"step": "route", "port": "true"}, "target": {"step": "gate", "port": "when"}},
            # `after` has no `when`: it is skipped only because the data it reads never came.
            {"source": {"step": "gate", "port": "decision"}, "target": {"step": "after", "port": "payload"}},
        ],
    }
    workflow = await _workflow(settings, "skip", definition)
    run = await engine.start_run(settings, TENANT, workflow, {"flag": False}, trigger="manual", started_by="")
    await engine.advance(settings, TENANT, run["id"])
    after = await run_store.get_run(settings, TENANT, run["id"])
    assert after is not None
    assert [after["steps"][s]["status"] for s in ("route", "gate", "after")] == [
        "completed",
        "skipped",
        "skipped",
    ]
    assert after["status"] == "completed"


def test_the_status_sets_cover_every_step_status() -> None:
    from typing import get_args

    assert run_store.STEP_IN_FLIGHT | run_store.STEP_TERMINAL | {"pending"} == set(
        get_args(run_store.StepStatus)
    )
    assert set(get_args(run_store.RunStatus)) > run_store.RUN_TERMINAL


async def test_stopping_a_step_answers_a_gate_its_agent_is_waiting_on() -> None:
    from felix.approvals import store as approvals

    settings = make_settings()
    run = await _start(settings)
    await engine.advance(settings, TENANT, run["id"])
    thread = (await run_store.get_run(settings, TENANT, run["id"]))["steps"]["write"]["thread_id"]  # type: ignore[index]
    # The child's own turn is parked on a tool approval.
    gate = await approvals.create_pending(
        settings,
        TENANT,
        tool_name="shell",
        call_signature="rm -rf build",
        thread_id=thread,
        tool_call_id="c1",
    )
    await engine.request_cancel(settings, TENANT, run["id"])
    await engine.advance(settings, TENANT, run["id"])
    closed = await approvals.get_approval(settings, TENANT, gate["id"])
    assert closed is not None
    assert (closed["status"], closed["decision_note"]) == ("denied", approvals.INTERRUPTED_NOTE), (
        "nobody can approve the tool after the run is gone"
    )


async def test_a_look_that_loses_to_a_run_ending_stops_what_it_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = make_settings()
    run = await _start(settings)

    async def ended_meanwhile(settings: Any, row: dict[str, Any]) -> bool:
        # While this look ran, the orchestrator was buried and the run closed.
        run_store._memory_runs[(TENANT, row["id"])]["status"] = "failed"
        return False

    monkeypatch.setattr(run_store, "save_run", ended_meanwhile)
    outcome = await engine.advance(settings, TENANT, run["id"])
    assert outcome.finished is True
    (child,) = _children(run["id"])
    assert child["status"] == "failed", "the child this look started does not outlive the run"


async def test_a_dead_orchestrator_fails_its_run_through_the_fiber_hook() -> None:
    settings = make_settings()
    run = await _start(settings)
    fiber = dict(F._memory_fibers[(TENANT, run["fiber_id"])], status="dead")
    await F._after_terminal(settings, fiber)
    after = await run_store.get_run(settings, TENANT, run["id"])
    assert after is not None and after["status"] == "failed"
