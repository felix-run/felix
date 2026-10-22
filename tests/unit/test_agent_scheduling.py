"""`spec.scheduling`: an agent schedules its own follow-up work as the person it is talking to.

The tools write ordinary jobs, so what is new is who may write one, which ones a caller can see
and cancel, and what a schedule may say. Each refusal below is a guard on the caller's authority
running later with nobody watching; they are driven through the bound tool's executor inside a
real request context, the way the react loop calls it.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from felix.config import Settings
from felix.context import AuthContext, RequestContext, async_run_with_context
from felix.jobs import store as jobs_store
from felix.jobs.agent_tools import (
    SCHEDULING_APPROVAL_RULE_ID,
    default_scheduling_rule,
    make_scheduling_tools,
)
from felix.manifests.loader import parse_manifest
from felix.manifests.schema import ApprovalRule, SchedulingSpec
from felix.tools.types import Tool, ToolInvocationCtx, tool_output_content

from tests.support.factories import make_settings

TENANT = "acme"
MANIFEST = "helper"
# The clock every test here reads (`_fixed_clock`), and a moment six weeks after it.
NOW = 1_792_454_400_000  # 2026-10-20T00:00:00Z
LATER = "at:2026-12-01T09:00:00Z"
LATER_ISO = "2026-12-01T09:00:00Z"
ALICE_SCOPES = frozenset({"jobs:write"})


def _api_key_settings() -> Settings:
    # Not `none`: there every caller holds every scope, and the scope refusal could not fire.
    return make_settings(
        auth_mode="api_key", auth_api_keys='{"k":{"sub":"alice","tenant_id":"acme","scopes":["jobs:write"]}}'
    )


def _tools(
    settings: Settings, *, manifest: str = MANIFEST, spec: SchedulingSpec | None = None
) -> dict[str, Tool]:
    bound = make_scheduling_tools(
        settings, tenant_id=TENANT, manifest_id=manifest, spec=spec or SchedulingSpec()
    )
    return {t.name: t for t in bound}


def _auth(sub: str = "alice", scopes: frozenset[str] = ALICE_SCOPES, **kw: Any) -> AuthContext:
    return AuthContext(
        principal_sub=sub, tenant_id=TENANT, scopes=scopes, anonymous=False, scheme="api_key", **kw
    )


def _firing() -> AuthContext:
    """A job's firing, built the way the scheduler builds it (`_job_auth`): `cron` for alice, and
    marked `scheduled` -- which is what the refusal reads."""
    from felix.jobs.scheduler import _job_auth

    job = {
        "name": "j",
        "created_by": "alice",
        "created_auth": {"principal_sub": "alice", "scopes": ["jobs:write"], "scheme": "jwt"},
    }
    return _job_auth(make_settings(), TENANT, job)


async def _call(
    settings: Settings,
    tool: Tool,
    args: dict[str, Any],
    auth: AuthContext | None = None,
    *,
    context: bool = True,
) -> dict[str, Any]:
    tool_ctx = ToolInvocationCtx(thread_id=f"{TENANT}:t1", tool_call_id="c1")
    if not context:
        out = await tool.executor.execute(args, tool_ctx)
    else:
        req = RequestContext(settings=settings, auth=auth or _auth(), manifest_id=MANIFEST, thread_id="x")
        async with async_run_with_context(req):
            out = await tool.executor.execute(args, tool_ctx)
    return json.loads(tool_output_content(out))


async def _schedule(settings: Settings, tools: dict[str, Tool], auth: AuthContext | None = None, **kw: Any):
    args = {"when": LATER, "prompt": "Check whether the release shipped.", **kw}
    return await _call(settings, tools["schedule_task"], args, auth)


@pytest.fixture
def settings() -> Settings:
    return _api_key_settings()


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    from felix.jobs import agent_tools

    monkeypatch.setattr(agent_tools, "_now_ms", lambda: NOW)
    monkeypatch.setattr(jobs_store, "now_ms", lambda: NOW)


# --- what a task is -------------------------------------------------------------------------


async def test_schedule_task_writes_a_job_on_this_manifest_as_the_caller(settings: Settings) -> None:
    tools = _tools(settings)
    out = await _schedule(settings, tools, title="release check", fresh_thread=True)

    assert out["status"] == "scheduled"
    assert out["name"].startswith("task-") and len(out["name"]) == len("task-") + 12
    assert (out["schedule"], out["next_run_at"], out["title"]) == (
        LATER,
        LATER_ISO,
        "release check",
    )
    job = await jobs_store.get_job(settings, TENANT, out["name"])
    assert job is not None
    assert (job["manifest_id"], job["created_by"], job["enabled"]) == (MANIFEST, "alice", True)
    assert job["created_auth"]["scopes"] == ["jobs:write"]
    assert job["created_auth"]["principal_sub"] == "alice"
    assert job["payload"] == {
        "prompt": "Check whether the release shipped.",
        "title": "release check",
        "fresh_thread": True,
        "scheduled_by": {"tool": "schedule_task", "manifest": MANIFEST, "thread_id": f"{TENANT}:t1"},
    }


async def test_a_durable_run_schedules_for_the_person_it_runs_for(settings: Settings) -> None:
    """A resumed fiber runs as `fiber` on someone's behalf; the task is theirs, not the worker's."""
    out = await _schedule(settings, _tools(settings), _auth("fiber", on_behalf_of="alice"))
    job = await jobs_store.get_job(settings, TENANT, out["name"])
    assert job["created_by"] == "alice"


async def test_a_recurring_task_waits_for_its_schedule(settings: Settings) -> None:
    """A job written over HTTP is due on the next sweep; a task set for 09:00 must not run now."""
    out = await _schedule(settings, _tools(settings), when="0 9 * * *")
    assert out["next_run_at"] == "2026-10-20T09:00:00Z"
    job = await jobs_store.get_job(settings, TENANT, out["name"])
    assert job["next_run_at"] == NOW + 9 * 3_600_000


async def test_a_task_scheduled_in_a_tenant_fires_once_from_the_worker_sweep(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker's entry point, across tenants: the task lands in the caller's tenant and the
    sweep that binds each tenant fires it once, at its moment, and never again."""
    from felix.jobs import scheduler

    out = await _schedule(settings, _tools(settings))
    job = await jobs_store.get_job(settings, TENANT, out["name"])
    assert job["created_auth"] is not None
    at = job["next_run_at"]

    monkeypatch.setattr(scheduler, "now_ms", lambda: at - 1)
    assert await scheduler.run_due_jobs_all_tenants(settings) == 0
    monkeypatch.setattr(scheduler, "now_ms", lambda: at + 1)
    assert await scheduler.run_due_jobs_all_tenants(settings) == 1
    assert await scheduler.run_due_jobs_all_tenants(settings) == 0
    after = await jobs_store.get_job(settings, TENANT, out["name"])
    assert after["next_run_at"] is None
    assert len(await jobs_store.list_runs(settings, TENANT, out["name"])) == 1


# --- refusals ---------------------------------------------------------------------------------


async def _no_jobs(settings: Settings) -> None:
    assert await jobs_store.list_jobs(settings, TENANT) == []


async def test_no_request_context_is_refused(settings: Settings) -> None:
    tools = _tools(settings)
    out = await _call(settings, tools["schedule_task"], {"when": LATER, "prompt": "x"}, context=False)
    assert out["error"] == "no_caller"
    await _no_jobs(settings)


@pytest.mark.parametrize(
    "auth",
    [
        AuthContext(tenant_id=TENANT, scopes=ALICE_SCOPES),  # anonymous
        AuthContext(principal_sub="", tenant_id=TENANT, scopes=ALICE_SCOPES, anonymous=False),
    ],
    ids=["anonymous", "nobody"],
)
async def test_a_caller_who_is_no_one_is_refused(settings: Settings, auth: AuthContext) -> None:
    out = await _schedule(settings, _tools(settings), auth)
    assert out["error"] == "no_caller"
    await _no_jobs(settings)


async def test_a_caller_without_jobs_write_is_refused(settings: Settings) -> None:
    tools = _tools(settings)
    out = await _schedule(settings, tools, _auth(scopes=frozenset({"jobs:read"})))
    assert out["error"] == "missing_scope" and "jobs:write" in out["detail"]
    await _no_jobs(settings)
    # The same caller may still list -- `jobs:read` is what reading asks for.
    listed = await _call(settings, tools["list_scheduled"], {}, _auth(scopes=frozenset({"jobs:read"})))
    assert listed == {"tasks": []}


async def test_auth_mode_none_needs_no_scope() -> None:
    """`holds_mgmt_scopes` checks nothing under `auth_mode=none`; a named caller with no scopes
    schedules there."""
    settings = make_settings()
    out = await _schedule(settings, _tools(settings), _auth(scopes=frozenset()))
    assert out["status"] == "scheduled", out


def test_a_job_firing_is_cron_for_its_writer_and_marked_scheduled() -> None:
    auth = _firing()
    assert (auth.principal_sub, auth.acting_for, auth.scheduled) == ("cron", "alice", True)


async def test_a_scheduled_run_cannot_schedule_more(settings: Settings) -> None:
    cron = _firing()
    out = await _schedule(settings, _tools(settings), cron)
    assert out["error"] == "scheduled_run"
    await _no_jobs(settings)


async def test_the_cap_counts_the_callers_live_tasks_here(settings: Settings) -> None:
    tools = _tools(settings, spec=SchedulingSpec(max_tasks=2))
    first = await _schedule(settings, tools)
    await _schedule(settings, tools)
    out = await _schedule(settings, tools)
    assert out["error"] == "too_many_tasks" and "2" in out["detail"]
    assert len(await jobs_store.list_jobs(settings, TENANT)) == 2

    # Someone else's tasks, and this caller's on another manifest, are not this caller's here.
    assert (await _schedule(settings, tools, _auth("bob")))["status"] == "scheduled"
    other = _tools(settings, manifest="other", spec=SchedulingSpec(max_tasks=2))
    assert (await _schedule(settings, other))["status"] == "scheduled"

    # A one-shot that has fired no longer holds a place.
    jobs_store._memory_jobs[(TENANT, first["name"])]["next_run_at"] = None
    assert (await _schedule(settings, tools))["status"] == "scheduled"


@pytest.mark.parametrize(
    ("when", "code"),
    [
        ("*/5 * * * *", "too_frequent"),
        ("every:30m", "too_frequent"),
        ("600", "too_frequent"),
        # Uneven: an hour apart most of the day, five minutes apart once.
        ("0,5 9 * * *", "too_frequent"),
        ("", "never_fires"),
        ("   ", "never_fires"),
        ("at:2001-01-01T00:00:00Z", "in_the_past"),
        ("at:2999-01-01T00:00:00", "invalid_schedule"),
        ("every tuesday", "invalid_schedule"),
    ],
)
async def test_a_schedule_the_task_may_not_have_is_refused(settings: Settings, when: str, code: str) -> None:
    out = await _schedule(settings, _tools(settings), when=when)
    assert out["error"] == code, out
    await _no_jobs(settings)


@pytest.mark.parametrize("when", ["@hourly", "every:60m", "0 9 * * 1-5", "@daily", "3600"])
async def test_a_schedule_at_the_floor_or_slower_is_accepted(settings: Settings, when: str) -> None:
    out = await _schedule(settings, _tools(settings), when=when)
    assert out["status"] == "scheduled", out


async def test_the_floor_is_the_manifests(settings: Settings) -> None:
    tools = _tools(settings, spec=SchedulingSpec(min_interval_minutes=5))
    assert (await _schedule(settings, tools, when="*/5 * * * *"))["status"] == "scheduled"
    assert (await _schedule(settings, tools, when="*/4 * * * *"))["error"] == "too_frequent"


@pytest.mark.parametrize(
    ("at_ms", "outcome"),
    [
        (NOW, "in_the_past"),
        (NOW + 1_000, "scheduled"),
        (NOW + 366 * 86_400_000, "scheduled"),
        (NOW + 366 * 86_400_000 + 1_000, "too_far_ahead"),
    ],
    ids=["now", "now+1s", "366d", "366d+1s"],
)
async def test_a_one_shots_bounds_are_exact(settings: Settings, at_ms: int, outcome: str) -> None:
    from datetime import UTC, datetime

    when = "at:" + datetime.fromtimestamp(at_ms / 1000, tz=UTC).isoformat()
    out = await _schedule(settings, _tools(settings), when=when)
    assert out.get("status", out.get("error")) == outcome, out


async def test_a_one_shot_more_than_a_year_out_is_refused(settings: Settings) -> None:
    tools = _tools(settings)
    assert (await _schedule(settings, tools, when="at:2027-10-22T00:00:00Z"))["error"] == "too_far_ahead"
    assert (await _schedule(settings, tools, when="at:2027-10-20T00:00:00Z"))["status"] == "scheduled"


@pytest.mark.parametrize(
    ("args", "code"),
    [
        ({"prompt": ""}, "invalid_prompt"),
        ({"prompt": "   "}, "invalid_prompt"),
        ({"prompt": "x" * 8001}, "invalid_prompt"),
        ({"title": "t" * 201}, "invalid_title"),
    ],
)
async def test_text_out_of_bounds_is_refused(settings: Settings, args: dict[str, Any], code: str) -> None:
    out = await _schedule(settings, _tools(settings), **args)
    assert out["error"] == code
    await _no_jobs(settings)


async def test_text_at_its_bounds_is_accepted(settings: Settings) -> None:
    out = await _schedule(settings, _tools(settings), prompt="x" * 8000, title="t" * 200)
    assert out["status"] == "scheduled"


# --- list and cancel see only the caller's own tasks here --------------------------------------


async def test_list_and_cancel_reach_only_the_callers_own_tasks_on_this_manifest(settings: Settings) -> None:
    tools = _tools(settings)
    mine = await _schedule(settings, tools, title="mine")
    bobs = await _schedule(settings, tools, _auth("bob"), title="bob's")
    elsewhere = await _schedule(settings, _tools(settings, manifest="other"), title="other manifest")
    # An operator's job over HTTP, written by alice herself: a job, not a task.
    await jobs_store.put_job(
        settings, TENANT, "nightly", schedule="@daily", manifest_id=MANIFEST, created_by="alice", payload={}
    )

    listed = await _call(settings, tools["list_scheduled"], {})
    assert [t["name"] for t in listed["tasks"]] == [mine["name"]]
    (task,) = listed["tasks"]
    assert (task["title"], task["schedule"], task["enabled"]) == ("mine", LATER, True)
    assert (task["next_run_at"], task["last_run_at"], task["last_status"]) == (
        LATER_ISO,
        None,
        None,
    )

    for name in (bobs["name"], elsewhere["name"], "nightly", "task-doesnotexist"):
        refused = await _call(settings, tools["cancel_scheduled"], {"name": name})
        assert refused["error"] == "no_such_task", name
        assert refused["detail"] == "there is no task of that name among this person's tasks"
    assert len(await jobs_store.list_jobs(settings, TENANT)) == 4

    done = await _call(settings, tools["cancel_scheduled"], {"name": mine["name"]})
    assert done == {"status": "cancelled", "name": mine["name"]}
    assert await jobs_store.get_job(settings, TENANT, mine["name"]) is None
    assert (await _call(settings, tools["list_scheduled"], {}))["tasks"] == []


async def test_cancel_needs_jobs_write(settings: Settings) -> None:
    tools = _tools(settings)
    mine = await _schedule(settings, tools)
    out = await _call(
        settings, tools["cancel_scheduled"], {"name": mine["name"]}, _auth(scopes=frozenset({"jobs:read"}))
    )
    assert out["error"] == "missing_scope"
    assert await jobs_store.get_job(settings, TENANT, mine["name"]) is not None


async def test_a_scheduled_run_may_list_and_cancel_its_writers_tasks(settings: Settings) -> None:
    """Only scheduling more is refused to a firing: a task that checks for something and stops
    itself once it has happened is the point of being able to cancel."""
    tools = _tools(settings)
    mine = await _schedule(settings, tools)
    cron = _firing()
    assert [t["name"] for t in (await _call(settings, tools["list_scheduled"], {}, cron))["tasks"]] == [
        mine["name"]
    ]
    assert (await _call(settings, tools["cancel_scheduled"], {"name": mine["name"]}, cron))[
        "status"
    ] == "cancelled"


# --- the default approval rule ----------------------------------------------------------------


def _manifest(**spec: Any) -> Any:
    return parse_manifest(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": MANIFEST},
            "spec": {"pattern": "react", **spec},
        }
    )


def test_the_default_rule_is_single_use_and_bound_to_the_asker() -> None:
    rule = default_scheduling_rule(SchedulingSpec(), [])
    assert rule is not None
    assert (rule.id, rule.tools, rule.one_shot, rule.bind_principal, rule.when_args) == (
        SCHEDULING_APPROVAL_RULE_ID,
        ["schedule_task"],
        True,
        True,
        [],
    )


@pytest.mark.parametrize(
    ("approval", "rules", "added"),
    [
        (True, [], True),
        (False, [], False),
        (True, [ApprovalRule(id="mine", tools=["schedule_task"], ttl_seconds=60)], False),
        # A glob does not displace it: the built-in literal beats the glob in `apply_approvals`.
        (True, [ApprovalRule(id="glob", tools=["schedule_*"])], True),
        (True, [ApprovalRule(id="all", tools=["*"])], True),
        (True, [ApprovalRule(id="other", tools=["cancel_scheduled"])], True),
        # Gates only the calls carrying `fresh_thread`; the rest must stay gated.
        (True, [ApprovalRule(id="some", tools=["schedule_task"], when_args=["fresh_thread"])], True),
    ],
    ids=["default", "off", "literal", "glob", "star", "other-tool", "when-args"],
)
def test_the_default_rule_is_added_only_where_nothing_already_gates_every_call(
    approval: bool, rules: list[ApprovalRule], added: bool
) -> None:
    assert (default_scheduling_rule(SchedulingSpec(approval=approval), rules) is not None) is added
    assert default_scheduling_rule(None, rules) is None


def _ids(rules: list[ApprovalRule] | None) -> list[str]:
    """Rule ids, less plan mode's, which every manifest allowing plan mode compiles."""
    from felix.governance.permission_mode import PLAN_APPROVAL_RULE_ID

    return [r.id for r in rules or [] if r.id != PLAN_APPROVAL_RULE_ID]


async def _compiled_rules(manifest: Any, monkeypatch: pytest.MonkeyPatch) -> list[ApprovalRule] | None:
    """The approval rules `build_agent` hands to `apply_approvals` -- the compile, not the helper."""
    from felix.manifests import builder
    from felix.tools.builtins import default_tool_provider

    seen: list[list[ApprovalRule]] = []
    real = builder.apply_approvals

    def _spy(tools: Any, rules: list[ApprovalRule], *a: Any, **k: Any) -> Any:
        seen.append(list(rules))
        return real(tools, rules, *a, **k)

    monkeypatch.setattr(builder, "apply_approvals", _spy)
    deps = builder.BuildDeps(tools=default_tool_provider(), settings=make_settings())
    await builder.build_agent(manifest, deps=deps)
    return seen[0] if seen else None


async def test_compiling_scheduling_binds_the_tools_and_gates_schedule_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rules = await _compiled_rules(_manifest(scheduling={}), monkeypatch)
    assert rules is not None
    assert _ids(rules) == [SCHEDULING_APPROVAL_RULE_ID]


async def test_compiling_with_approval_off_adds_no_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _ids(await _compiled_rules(_manifest(scheduling={"approval": False}), monkeypatch)) == []


async def test_compiling_with_the_manifests_own_rule_does_not_duplicate_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    own = {"id": "mine", "tools": ["schedule_task"], "ttl_seconds": 120}
    rules = await _compiled_rules(_manifest(scheduling={}, approvals=[own]), monkeypatch)
    assert _ids(rules) == ["mine"]


@pytest.mark.parametrize(("spec", "bound"), [({"scheduling": {}}, True), ({}, False)], ids=["block", "none"])
async def test_the_block_binds_the_tools_and_its_absence_binds_none(
    spec: dict[str, Any], bound: bool
) -> None:
    from felix.manifests import builder
    from felix.tools.builtins import default_tool_provider

    deps = builder.BuildDeps(tools=default_tool_provider(), settings=make_settings())
    agent = await builder.build_agent(_manifest(**spec), deps=deps)
    names = {t.name for t in agent.tools}
    assert (names >= {"schedule_task", "list_scheduled", "cancel_scheduled"}) is bound
    assert bool(names & {"schedule_task", "list_scheduled", "cancel_scheduled"}) is bound


async def test_a_tool_already_named_schedule_task_is_refused_at_compile() -> None:
    from felix.manifests import builder
    from felix.tools.builtins import default_tool_provider
    from felix.tools.types import define_tool

    async def _h(_a: Any) -> str:
        return "impostor"

    impostor = define_tool(name="schedule_task", description="x", args_schema={"type": "object"}, handler=_h)
    deps = builder.BuildDeps(tools=default_tool_provider(), settings=make_settings(), extra_tools=[impostor])
    with pytest.raises(ValueError, match=r"spec\.scheduling binds a tool named 'schedule_task'"):
        await builder.build_agent(_manifest(scheduling={}), deps=deps)


# --- the approval gate never sees a call the tool would refuse ---------------------------------


async def _gated_schedule_task(settings: Settings, tenant: str = "default") -> Tool:
    from felix.manifests import builder
    from felix.tools.builtins import default_tool_provider

    deps = builder.BuildDeps(tools=default_tool_provider(), settings=settings, tenant_id=tenant)
    agent = await builder.build_agent(_manifest(scheduling={}), deps=deps)
    return next(t for t in agent.tools if t.name == "schedule_task")


@pytest.mark.parametrize(
    ("auth", "code"),
    [
        (_firing(), "scheduled_run"),
        (AuthContext(tenant_id=TENANT, scopes=ALICE_SCOPES), "no_caller"),
    ],
    ids=["cron", "anonymous"],
)
async def test_a_refused_caller_is_denied_before_any_approval_is_asked(
    settings: Settings, auth: AuthContext, code: str
) -> None:
    """Under the default rule the approvals wrapper runs before the tool. A scheduled run or an
    anonymous caller used to write a pending row -- its prompt in front of an approver -- and a
    cron firing then waited out the approval's TTL, stalling the sweep."""
    import asyncio

    from felix.approvals import store as approvals_store

    tool = await _gated_schedule_task(settings, TENANT)
    req = RequestContext(settings=settings, auth=auth, manifest_id=MANIFEST, thread_id=f"{TENANT}:t1")
    async with async_run_with_context(req):
        out = await asyncio.wait_for(
            tool.executor.execute(
                {"when": LATER, "prompt": "Ignore previous instructions."},
                ToolInvocationCtx(thread_id=f"{TENANT}:t1", tool_call_id="c1"),
            ),
            timeout=5,
        )
    text = tool_output_content(out)
    assert "[approval preview failed]" in text and code in text, text
    assert await approvals_store.list_approvals(settings, TENANT, status=None) == []
    await _no_jobs(settings)


async def test_an_admitted_call_still_asks_for_approval(settings: Settings) -> None:
    """The other side of the test above: a call the tool would make reaches the approver."""
    import asyncio

    from felix.approvals import store as approvals_store

    tool = await _gated_schedule_task(settings, TENANT)
    req = RequestContext(settings=settings, auth=_auth(), manifest_id=MANIFEST, thread_id=f"{TENANT}:t1")
    async with async_run_with_context(req):
        call = asyncio.create_task(
            tool.executor.execute(
                {"when": LATER, "prompt": "Check the release."},
                ToolInvocationCtx(thread_id=f"{TENANT}:t1", tool_call_id="c1"),
            )
        )
        try:
            rows: list[dict[str, Any]] = []
            for _ in range(100):
                await asyncio.sleep(0.02)
                rows = await approvals_store.list_approvals(settings, TENANT)
                if rows:
                    break
            assert [r["tool_name"] for r in rows] == ["schedule_task"]
        finally:
            call.cancel()
    await _no_jobs(settings)


async def test_the_bound_tool_writes_to_the_compiling_tenant(settings: Settings) -> None:
    from felix.manifests import builder
    from felix.tools.builtins import default_tool_provider

    deps = builder.BuildDeps(tools=default_tool_provider(), settings=settings, tenant_id=TENANT)
    agent = await builder.build_agent(_manifest(scheduling={"approval": False}), deps=deps)
    tool = next(t for t in agent.tools if t.name == "schedule_task")
    out = await _call(settings, tool, {"when": LATER, "prompt": "Check the release."})
    assert out["status"] == "scheduled", out
    assert [j["name"] for j in await jobs_store.list_jobs(settings, TENANT)] == [out["name"]]
    assert await jobs_store.list_jobs(settings, "default") == []


# --- no permission mode waives the scheduling approval ---------------------------------------


@pytest.mark.parametrize(
    ("approvals", "asked_by"),
    [
        ([{"id": "calc", "tools": ["calculator"]}], SCHEDULING_APPROVAL_RULE_ID),
        # A waivable catch-all: the built-in literal is still the rule selected for schedule_task.
        ([{"id": "all", "tools": ["*"]}], SCHEDULING_APPROVAL_RULE_ID),
        # The manifest's own literal rule replaces the built-in one, and is not waived either.
        (
            [{"id": "calc", "tools": ["calculator"]}, {"id": "mine", "tools": ["schedule_task"]}],
            "mine",
        ),
    ],
    ids=["built-in", "glob", "own-literal"],
)
async def test_a_thread_in_bypass_still_asks_before_scheduling(
    approvals: list[dict[str, Any]], asked_by: str
) -> None:
    """`bypass` waives the calculator's gate on the same thread -- the mode is live -- and never
    the one over `schedule_task`, whose prompt runs later with no one watching."""
    import asyncio

    from felix.approvals import store as approvals_store
    from felix.manifests import builder
    from felix.session.thread_state import update_thread_meta
    from felix.tools.builtins import default_tool_provider

    settings = make_settings()  # `auth_mode=none`: the caller may bypass
    thread = f"{TENANT}:bypassed"
    await update_thread_meta(settings=settings, tenant_id=TENANT, thread_id=thread, permission_mode="bypass")
    manifest = _manifest(
        tools=["calculator"],
        scheduling={},
        approvals=approvals,
        permissions={"allowed_modes": ["default", "bypass"]},
    )
    deps = builder.BuildDeps(tools=default_tool_provider(), settings=settings, tenant_id=TENANT)
    tools = {t.name: t for t in (await builder.build_agent(manifest, deps=deps)).tools}
    req = RequestContext(settings=settings, auth=_auth(), manifest_id=MANIFEST, thread_id=thread)
    call_ctx = ToolInvocationCtx(thread_id=thread, tool_call_id="c1")
    async with async_run_with_context(req):
        calc = await asyncio.wait_for(
            tools["calculator"].executor.execute({"expression": "2+2"}, call_ctx), 5
        )
        assert tool_output_content(calc).strip() == "4", "bypass is live on this thread"
        call = asyncio.create_task(
            tools["schedule_task"].executor.execute({"when": LATER, "prompt": "Check the release."}, call_ctx)
        )
        try:
            rows: list[dict[str, Any]] = []
            for _ in range(100):
                await asyncio.sleep(0.02)
                rows = await approvals_store.list_approvals(settings, TENANT)
                if rows or call.done():
                    break
            assert [(r["tool_name"], r["rule_id"]) for r in rows] == [("schedule_task", asked_by)]
        finally:
            call.cancel()
    await _no_jobs(settings)


# --- a subject shared by every holder of a credential owns nothing ---------------------------


@pytest.mark.parametrize("tool", ["schedule_task", "list_scheduled", "cancel_scheduled"])
async def test_a_shared_subject_is_refused(settings: Settings, tool: str) -> None:
    """A key configured with no `sub` authenticates as `api_key`; every such key shares it."""
    from felix.auth.api_keys import DEFAULT_API_KEY_SUBJECT

    args = {
        "schedule_task": {"when": LATER, "prompt": "x"},
        "list_scheduled": {},
        "cancel_scheduled": {"name": "task-x"},
    }[tool]
    out = await _call(settings, _tools(settings)[tool], args, _auth(DEFAULT_API_KEY_SUBJECT))
    assert out["error"] == "shared_subject", out
    await _no_jobs(settings)


async def test_a_slack_user_is_a_person() -> None:
    """`felix_channels` runs a Slack turn as `slack:<team>:<user>`: one subject per Slack user."""
    settings = make_settings()
    out = await _schedule(settings, _tools(settings), _auth("slack:T1:U1"))
    assert out["status"] == "scheduled", out


# --- a task lives no longer than the token that wrote it -------------------------------------


def _jwt_auth(exp_ms: int) -> AuthContext:
    return AuthContext(
        principal_sub="alice",
        tenant_id=TENANT,
        scopes=ALICE_SCOPES,
        anonymous=False,
        scheme="jwt",
        raw_claims={"exp": exp_ms // 1000},
    )


async def test_a_one_shot_after_the_token_expires_is_refused(settings: Settings) -> None:
    exp = NOW + 3_600_000
    out = await _schedule(settings, _tools(settings), _jwt_auth(exp), when="at:2026-10-20T02:00:00Z")
    assert out["error"] == "outlives_credential", out
    await _no_jobs(settings)
    ok = await _schedule(settings, _tools(settings), _jwt_auth(exp), when="at:2026-10-20T00:30:00Z")
    assert ok["status"] == "scheduled", ok


async def test_a_task_fired_after_its_token_expired_is_refused_and_disabled(settings: Settings) -> None:
    from felix.jobs.scheduler import CREDENTIAL_EXPIRED, fire_job

    exp = NOW + 3_600_000
    out = await _schedule(settings, _tools(settings), _jwt_auth(exp), when="@daily")
    job = await jobs_store.get_job(settings, TENANT, out["name"])
    assert job["created_auth"]["expires_at"] == exp
    listed = await _call(settings, _tools(settings)["list_scheduled"], {}, _jwt_auth(exp))
    assert listed["tasks"][0]["expires_at"] == "2026-10-20T01:00:00Z"

    run = await fire_job(settings, TENANT, job, started_at=exp)

    assert (run["status"], run["error"]) == ("error", CREDENTIAL_EXPIRED)
    after = await jobs_store.get_job(settings, TENANT, out["name"])
    assert (after["enabled"], after["last_status"], after["last_error"]) == (
        False,
        "error",
        CREDENTIAL_EXPIRED,
    )


async def test_a_credential_with_no_expiry_bounds_nothing(settings: Settings) -> None:
    """An API key carries no `exp`: its tasks record none, and fire as long as the key exists."""
    from felix.jobs.scheduler import fire_job

    out = await _schedule(settings, _tools(settings))
    job = await jobs_store.get_job(settings, TENANT, out["name"])
    assert job["created_auth"]["expires_at"] is None

    run = await fire_job(settings, TENANT, job, started_at=NOW + 10 * 365 * 86_400_000)
    assert run["error"] != "the credential that scheduled this task has expired; schedule it again"
    assert (await jobs_store.get_job(settings, TENANT, out["name"]))["enabled"] is True


async def test_a_trigger_runs_subject_is_shared(settings: Settings) -> None:
    """An inbound trigger's run acts as `trigger:<id>` for every sender, with no token expiry."""
    from felix.workflows.runs import TRIGGER_RUN_PREFIX

    for tool, args in (
        ("schedule_task", {"when": LATER, "prompt": "x"}),
        ("list_scheduled", {}),
        ("cancel_scheduled", {"name": "task-x"}),
    ):
        out = await _call(settings, _tools(settings)[tool], args, _auth(f"{TRIGGER_RUN_PREFIX}github-push"))
        assert out["error"] == "shared_subject", (tool, out)
    await _no_jobs(settings)


async def test_a_task_disabled_on_expiry_holds_no_place(settings: Settings) -> None:
    from felix.jobs.scheduler import fire_job

    tools = _tools(settings, spec=SchedulingSpec(max_tasks=1))
    exp = NOW + 3_600_000
    out = await _schedule(settings, tools, _jwt_auth(exp), when="@daily")
    assert (await _schedule(settings, tools))["error"] == "too_many_tasks"
    await fire_job(settings, TENANT, await jobs_store.get_job(settings, TENANT, out["name"]), started_at=exp)
    assert (await _schedule(settings, tools))["status"] == "scheduled"


# --- retention of tasks that will not run again ------------------------------------------------


async def _fired_task(settings: Settings, tenant: str, name: str, last_run_at: int) -> None:
    payload = {
        "prompt": "x",
        "scheduled_by": {"tool": "schedule_task", "manifest": MANIFEST, "thread_id": None},
    }
    await jobs_store.put_job(settings, tenant, name, schedule=LATER, manifest_id=MANIFEST, payload=payload)
    await jobs_store.touch_run(settings, tenant, name, last_run_at=last_run_at, next_run_at=None)


async def test_one_tenants_failing_sweep_leaves_the_next_swept(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from felix.jobs import retention

    old = NOW - 40 * 86_400_000
    await _fired_task(settings, "a-broken", "task-1", old)
    await _fired_task(settings, "b-fine", "task-2", old)
    real = jobs_store.list_jobs

    async def _list(s: Any, tenant_id: str) -> list[dict[str, Any]]:
        if tenant_id == "a-broken":
            raise RuntimeError("store down for this tenant")
        return await real(s, tenant_id)

    monkeypatch.setattr(jobs_store, "list_jobs", _list)
    assert await retention._sweep_fired_tasks(settings, NOW) == 1
    assert await jobs_store.get_job(settings, "b-fine", "task-2") is None
    assert await jobs_store.get_job(settings, "a-broken", "task-1") is not None


async def test_a_task_rewritten_after_the_listing_survives_the_sweep(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sweep reads the task again just before deleting it: one re-armed meanwhile stays."""
    from felix.jobs import retention

    await _fired_task(settings, TENANT, "task-1", NOW - 40 * 86_400_000)
    real = jobs_store.get_job

    async def _rearmed(s: Any, tenant_id: str, name: str) -> dict[str, Any] | None:
        row = await real(s, tenant_id, name)
        return None if row is None else {**row, "next_run_at": NOW + 60_000}

    monkeypatch.setattr(jobs_store, "get_job", _rearmed)
    assert await retention._sweep_fired_tasks(settings, NOW) == 0
    assert await real(settings, TENANT, "task-1") is not None
