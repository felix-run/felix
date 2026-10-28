"""A task an agent scheduled reports its answer into the conversation it was scheduled from.

Through the production app: a chat turn whose model calls `schedule_task`, the worker's sweep firing
the task on its own thread, and a later turn in the first conversation -- the model scripted. Before
this, the firing's answer went only to the job's run history, and the conversation that asked for
it never heard back unless the task happened to send a `notify`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from felix.manifests.loader import parse_manifest
from felix_ai.providers.scripted import ScriptedTurn
from felix_ai.types import ToolCall

KEYS = {"sk-e2e-alice": {"tenant_id": "default", "sub": "alice", "scopes": ["jobs:write", "jobs:read"]}}
ENV = {"FELIX_AUTH_MODE": "api_key", "FELIX_AUTH_API_KEYS": json.dumps(KEYS)}
ALICE = {"Authorization": "Bearer sk-e2e-alice"}
THREAD = "release-chat"
ANSWER = "The release shipped at 10:02 UTC."


def _manifest(**scheduling: Any) -> Any:
    spec = {"pattern": "react", "scheduling": {"approval": False, **scheduling}}
    return parse_manifest(
        {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": "helper"}, "spec": spec}
    )


def _script() -> list[ScriptedTurn]:
    when = (datetime.now(UTC) + timedelta(days=1)).strftime("at:%Y-%m-%dT%H:%M:%SZ")
    args = {"when": when, "prompt": "Check whether the release shipped.", "title": "Release check"}
    return [
        ScriptedTurn(tool_calls=[ToolCall(id="c1", name="schedule_task", args=args)]),
        ScriptedTurn(content="I'll check tomorrow."),
        ScriptedTurn(content=ANSWER),  # the firing
        ScriptedTurn(content="It shipped at 10:02."),  # the next turn in the conversation
    ]


async def _turn(app: Any, text: str) -> None:
    resp = await app.client.post(
        "/chat",
        json={"manifest": "helper", "thread_id": THREAD, "messages": [{"role": "user", "content": text}]},
        headers=ALICE,
    )
    assert resp.status_code == 200, resp.text


async def _fire(app: Any, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Run the worker's sweep a moment after the task is due; the run it recorded."""
    from felix.jobs import scheduler
    from felix.jobs import store as jobs_store

    (job,) = await jobs_store.list_jobs(app.settings, "default")
    monkeypatch.setattr(scheduler, "now_ms", lambda: job["next_run_at"] + 1)
    assert await scheduler.run_due_jobs_all_tenants(app.settings) == 1
    (run,) = await jobs_store.list_runs(app.settings, "default", job["name"])
    return run


async def _reports(app: Any) -> list[Any]:
    from felix.session.store import get_session_store
    from felix.session.tree import active_branch_events, stored_leaf

    session = get_session_store(app.settings, tenant_id="default").open(f"default:{THREAD}")
    events = await session.get_events()
    branch = active_branch_events(events, leaf_id=await stored_leaf(session))
    return [e for e in branch if (e.metadata or {}).get("type") == "task_report"]


async def test_the_answer_lands_in_the_conversation_and_the_next_turn_reads_it(
    boot: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with boot(_script(), env=ENV, manifests={"helper": _manifest()}) as app:
        await _turn(app, "Tell me tomorrow whether the release shipped.")
        run = await _fire(app, monkeypatch)
        reports = await _reports(app)
        await _turn(app, "When did it ship?")
        prompt = app.spy.prompts[-1]

    assert (run["status"], run["result"]["answer"], run["result"]["report"]) == ("ok", ANSWER, "appended")
    (report,) = reports
    assert (report.role, report.content) == ("assistant", f"[Scheduled task: Release check]\n\n{ANSWER}")
    seen = [(m.role, str(m.content)) for m in prompt]
    assert ("assistant", f"[Scheduled task: Release check]\n\n{ANSWER}") in seen, seen
    # In order: after the turn that scheduled it, before the question about it.
    where = seen.index(("assistant", report.content))
    assert (
        seen.index(("assistant", "I'll check tomorrow.")) < where < seen.index(("user", "When did it ship?"))
    )


async def test_report_back_off_leaves_the_conversation_alone(
    boot: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with boot(_script(), env=ENV, manifests={"helper": _manifest(report_back=False)}) as app:
        await _turn(app, "Tell me tomorrow whether the release shipped.")
        run = await _fire(app, monkeypatch)
        reports = await _reports(app)

    assert run["result"]["answer"] == ANSWER
    assert "report" not in run["result"]
    assert reports == []


async def test_a_report_kept_out_by_a_live_run_lands_when_a_turn_there_ends(
    boot: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The firing finds a run going on in the conversation (marked as a turn marks it); the report
    waits on the thread, and the end of the next turn there adds it -- the turn loop's own settle,
    not a test helper."""
    from felix.workspace_notes import mark_run_active, mark_run_idle

    script = [*_script()[:3], ScriptedTurn(content="Yes, it's out."), ScriptedTurn(content="10:02.")]
    async with boot(script, env=ENV, manifests={"helper": _manifest()}) as app:
        await _turn(app, "Tell me tomorrow whether the release shipped.")
        await mark_run_active("default", f"default:{THREAD}")
        try:
            run = await _fire(app, monkeypatch)
            held = await _reports(app)
        finally:
            await mark_run_idle("default", f"default:{THREAD}")
        await _turn(app, "Anything else?")
        landed = await _reports(app)
        await _turn(app, "When did it ship?")
        prompt = app.spy.prompts[-1]

    assert run["result"]["report"] == "queued"
    assert held == []
    (report,) = landed
    assert report.content == f"[Scheduled task: Release check]\n\n{ANSWER}"
    seen = [(m.role, str(m.content)) for m in prompt]
    assert ("assistant", report.content) in seen, seen


async def test_a_task_that_fails_says_so_in_the_conversation(
    boot: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = [
        *_script()[:2],
        ScriptedTurn(error=RuntimeError("upstream exploded")),
        ScriptedTurn(content="ok"),
    ]
    async with boot(script, env=ENV, manifests={"helper": _manifest()}) as app:
        await _turn(app, "Tell me tomorrow whether the release shipped.")
        run = await _fire(app, monkeypatch)
        reports = await _reports(app)

    assert run["status"] == "error"
    assert run["result"]["report"] == "appended"
    (report,) = reports
    assert report.content.startswith("[Scheduled task: Release check]\n\nThe task did not finish: ")
    assert report.metadata["failed"] is True
    # Written for readers, never the exception's own text (`scheduler._job_error_message`).
    assert "upstream exploded" not in report.content and run["error"] in report.content


async def test_only_a_tasks_newest_report_reaches_the_next_turn(
    boot: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recurring task reports every firing; each report restates the task's answer, so only the
    newest stays in context. The conversation still shows both."""
    from felix.jobs import scheduler
    from felix.jobs import store as jobs_store

    newer = "The release shipped at 10:02 UTC; the hotfix is out too."
    script = [*_script()[:3], ScriptedTurn(content=newer), ScriptedTurn(content="It shipped.")]
    async with boot(script, env=ENV, manifests={"helper": _manifest()}) as app:
        await _turn(app, "Tell me tomorrow whether the release shipped.")
        await _fire(app, monkeypatch)
        (job,) = await jobs_store.list_jobs(app.settings, "default")
        await scheduler.fire_job(
            app.settings, "default", job, started_at=scheduler.now_ms(), trigger="manual"
        )
        reports = await _reports(app)
        await _turn(app, "When did it ship?")
        prompt = app.spy.prompts[-1]

    assert [r.content for r in reports] == [
        f"[Scheduled task: Release check]\n\n{ANSWER}",
        f"[Scheduled task: Release check]\n\n{newer}",
    ]
    seen = [str(m.content) for m in prompt]
    assert f"[Scheduled task: Release check]\n\n{newer}" in seen, seen
    assert f"[Scheduled task: Release check]\n\n{ANSWER}" not in seen, seen
