"""A run nobody is watching does not wait on a person.

A scheduled job's firing that reached an approval-gated tool wrote a pending row, pushed it to the
whole tenant, emitted a frame into a thread no one had open, and then blocked for the rule's ttl
(five minutes with none). `run_due_jobs` awaits each firing in turn, so every job behind it in the
sweep -- every tenant's, too -- waited with it. `ApprovalRule.allow_unattended` was read only by the
EU AI Act compile check, and `RequestContext.unattended` by nothing.

Now a context built from a scheduled caller is unattended (`RequestContext.__post_init__`), and in
one the gates that wait on a person refuse at once: an approval rule without `allow_unattended`, a
command-screening approval, a client tool. `ask_user` already refused off a live stream.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest
from felix.approvals import store as approvals_store
from felix.config import Settings
from felix.context import AuthContext, RequestContext, async_run_with_context
from felix.manifests.schema import ApprovalRule
from felix.tools.types import Tool, ToolInvocationCtx, define_tool, tool_output_content

from tests.support.factories import make_settings

TENANT = "default"
MANIFEST = "gated"
THREAD = f"{TENANT}:job:nightly"


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture(autouse=True)
def _clean() -> Any:
    approvals_store.reset_approvals_for_tests()
    yield
    approvals_store.reset_approvals_for_tests()


@pytest.fixture
def pushed(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every approval announced to the tenant's browsers (`push.notify.approval_pending`)."""
    from felix.push import notify

    calls: list[str] = []
    monkeypatch.setattr(notify, "approval_pending", lambda _settings, row: calls.append(str(row["id"])))
    return calls


def _firing() -> AuthContext:
    """A job's firing, built by the scheduler's own `_job_auth`: `cron` for alice, `scheduled`."""
    from felix.jobs.scheduler import _job_auth

    job = {
        "name": "nightly",
        "created_by": "alice",
        "created_auth": {"principal_sub": "alice", "scopes": ["jobs:write"], "scheme": "jwt"},
    }
    return _job_auth(make_settings(), TENANT, job)


def _person() -> AuthContext:
    return AuthContext(principal_sub="alice", tenant_id=TENANT, anonymous=False, scheme="jwt")


def _gated(ran: list[dict[str, Any]], **rule: Any) -> Tool:
    from felix.manifests.builder import apply_approvals

    async def _calc(args: dict[str, Any]) -> str:
        ran.append(args)
        return "2"

    (tool,) = apply_approvals(
        [define_tool(name="calculator", description="c", handler=_calc)],
        [ApprovalRule(id="calc-gate", tools=["calculator"], **rule)],
        MANIFEST,
    )
    return tool


async def _call(settings: Settings, auth: AuthContext, tool: Tool, *, within: float) -> tuple[str, float]:
    req = RequestContext(settings=settings, auth=auth, manifest_id=MANIFEST, thread_id=THREAD)
    started = time.monotonic()
    async with async_run_with_context(req):
        out = await asyncio.wait_for(
            tool.executor.execute(
                {"expression": "1+1"}, ToolInvocationCtx(thread_id=THREAD, tool_call_id="c1")
            ),
            timeout=within,
        )
    return tool_output_content(out), time.monotonic() - started


# --- what makes a run unattended ----------------------------------------------------------------


def test_a_scheduled_caller_makes_its_context_unattended_and_a_person_does_not(settings: Settings) -> None:
    assert RequestContext(settings=settings, auth=_firing()).unattended
    assert not RequestContext(settings=settings, auth=_person()).unattended


def test_an_explicit_mark_is_kept_and_a_triggers_mark_survives_the_record(settings: Settings) -> None:
    """A trigger's caller is `unattended` without being `scheduled` (that means "descends from a
    job"); its mark rides `record_caller` / `replay_caller` into the run it starts, only ever added."""
    from felix.auth.recorded import record_caller, replay_caller

    assert RequestContext(settings=settings, auth=_person(), unattended=True).unattended
    sender = AuthContext(principal_sub="trigger:gh", tenant_id=TENANT, scheme="trigger", unattended=True)
    replayed = replay_caller(record_caller(sender), tenant_id=TENANT, actor="fiber")
    assert (replayed.unattended, replayed.scheduled) == (True, False)
    assert RequestContext(settings=settings, auth=replayed).unattended
    assert not replay_caller(record_caller(_person()), tenant_id=TENANT, actor="fiber").unattended


def test_a_durable_child_of_a_scheduled_run_is_unattended(settings: Settings) -> None:
    """The run a firing starts records its caller (`durability.runs.record_caller`, which a durable
    chat, a background child and a workflow run all use); the worker rebuilds it
    (`fibers.auth_from_state`). The context it resumes under is unattended because the caller is
    still marked scheduled -- no builder of it sets the flag."""
    from felix.context import run_with_context
    from felix.durability.fibers import auth_from_state
    from felix.durability.runs import record_caller

    with run_with_context(RequestContext(settings=settings, auth=_firing())):
        recorded, _ = record_caller(TENANT, 10**15)
    resumed = RequestContext(settings=settings, auth=auth_from_state(TENANT, recorded))
    assert resumed.auth.principal_sub == "fiber" and resumed.auth.acting_for == "alice"
    assert resumed.unattended

    with run_with_context(RequestContext(settings=settings, auth=_person())):
        recorded, _ = record_caller(TENANT, 10**15)
    assert not RequestContext(settings=settings, auth=auth_from_state(TENANT, recorded)).unattended


# --- the approval gate --------------------------------------------------------------------------


async def test_an_unattended_call_is_refused_at_once_with_no_row_and_no_push(
    settings: Settings, pushed: list[str]
) -> None:
    ran: list[dict[str, Any]] = []
    text, took = await _call(settings, _firing(), _gated(ran, ttl_seconds=300), within=5)
    assert text == (
        "[approval unavailable] tool=calculator rule=calc-gate: this run is unattended "
        "(no person started it), so no one can approve it; the call was not made."
    )
    assert took < 2, "the refusal waited"
    assert ran == []
    assert await approvals_store.list_approvals(settings, TENANT, status=None) == []
    assert pushed == []


async def test_a_durable_child_of_a_scheduled_run_is_refused_too(settings: Settings) -> None:
    """The same gate, under the context a resumed child gets (see the test above)."""
    from felix.context import run_with_context
    from felix.durability.fibers import auth_from_state
    from felix.durability.runs import record_caller

    with run_with_context(RequestContext(settings=settings, auth=_firing())):
        recorded, _ = record_caller(TENANT, 10**15)
    ran: list[dict[str, Any]] = []
    text, _ = await _call(settings, auth_from_state(TENANT, recorded), _gated(ran, ttl_seconds=300), within=5)
    assert text.startswith("[approval unavailable]"), text
    assert ran == [] and await approvals_store.list_approvals(settings, TENANT, status=None) == []


async def test_allow_unattended_keeps_the_wait(settings: Settings, pushed: list[str]) -> None:
    """The operator said someone will answer even when no one started the run: it asks and waits."""
    ran: list[dict[str, Any]] = []
    text, took = await _call(
        settings, _firing(), _gated(ran, ttl_seconds=1, allow_unattended=True), within=10
    )
    assert text.startswith("[approval timeout]"), text
    assert took >= 0.9, "it did not wait out the ttl"
    (row,) = await approvals_store.list_approvals(settings, TENANT, status=None)
    assert (row["principal_subj"], row["rule_id"]) == ("alice", "calc-gate")
    assert pushed == [row["id"]]


async def test_an_interactive_call_still_asks_and_waits(settings: Settings, pushed: list[str]) -> None:
    ran: list[dict[str, Any]] = []
    text, took = await _call(settings, _person(), _gated(ran, ttl_seconds=1), within=10)
    assert text.startswith("[approval timeout]"), text
    assert took >= 0.9
    assert len(await approvals_store.list_approvals(settings, TENANT, status=None)) == 1
    assert len(pushed) == 1


async def test_a_grant_already_made_still_runs_unattended(settings: Settings) -> None:
    """Refusing is about asking: a call a person already approved, byte for byte, needs no one."""
    import hashlib

    ran: list[dict[str, Any]] = []
    tool = _gated(ran, ttl_seconds=300)
    sig = hashlib.sha256(json.dumps({"expression": "1+1"}, sort_keys=True).encode()).hexdigest()[:32]
    row = await approvals_store.create_pending(
        settings,
        TENANT,
        manifest_id=MANIFEST,
        tool_name="calculator",
        call_signature=sig,
        principal_subj="alice",
    )
    await approvals_store.decide(settings, TENANT, row["id"], decision="approved", decided_by="alice")
    text, _ = await _call(settings, _firing(), tool, within=5)
    assert text == "2" and ran == [{"expression": "1+1"}]


# --- the other gates that wait on a person -------------------------------------------------------


async def test_a_command_approval_in_an_unattended_run_is_refused_at_once(settings: Settings) -> None:
    from felix.manifests.builder import apply_command_screening
    from felix.manifests.schema import CommandRule, CommandScreening

    ran: list[dict[str, Any]] = []

    async def _run(args: dict[str, Any]) -> str:
        ran.append(args)
        return "ran"

    (tool,) = apply_command_screening(
        [define_tool(name="run", description="r", handler=_run)],
        CommandScreening(
            enabled=True,
            include_defaults=False,
            target_tools=["run"],
            rules=[CommandRule(pattern=r"\bsudo\b", decision="require_approval", reason="sudo")],
        ),
        MANIFEST,
    )
    req = RequestContext(settings=settings, auth=_firing(), manifest_id=MANIFEST, thread_id=THREAD)
    async with async_run_with_context(req):
        out = await asyncio.wait_for(
            tool.executor.execute(
                {"command": "sudo ls"}, ToolInvocationCtx(thread_id=THREAD, tool_call_id="c")
            ),
            timeout=5,
        )
    text = tool_output_content(out)
    assert text.startswith("[command approval unavailable: this run is unattended"), text
    assert ran == [] and await approvals_store.list_approvals(settings, TENANT, status=None) == []


async def _client_tool_call(settings: Settings, auth: AuthContext) -> str:
    from felix.manifests.schema import ClientToolRef
    from felix.tools.client_bridge import tools_from_client_refs

    (tool,) = tools_from_client_refs([ClientToolRef(name="pick_file", timeout_seconds=0.5)])
    req = RequestContext(settings=settings, auth=auth, manifest_id=MANIFEST, thread_id=THREAD)
    async with async_run_with_context(req):
        out = await asyncio.wait_for(
            tool.executor.execute({}, ToolInvocationCtx(thread_id=THREAD, tool_call_id="c1")), timeout=5
        )
    return tool_output_content(out)


async def test_a_client_tool_in_an_unattended_run_is_refused_without_a_request(settings: Settings) -> None:
    from felix.tools import client_requests

    recorded: list[Any] = []
    real = client_requests.record

    async def _spy(*a: Any, **kw: Any) -> None:
        recorded.append(a)
        await real(*a, **kw)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(client_requests, "record", _spy)
        refused = await _client_tool_call(settings, _firing())
        attended = await _client_tool_call(settings, _person())
    assert refused.startswith("[client tool unavailable] tool=pick_file: this run is unattended"), refused
    assert attended == "[error/timeout] client tool timed out", "a person's run still waits for its client"
    assert len(recorded) == 1, "only the attended call asked a client"


async def test_ask_user_in_a_scheduled_run_answers_at_once(settings: Settings) -> None:
    """Not changed here, and pinned: `ask_user` asks only over a live stream, which no firing has."""
    from felix.ui.ask_user import AskUserArgs, ask_user_handler

    req = RequestContext(settings=settings, auth=_firing(), manifest_id=MANIFEST, thread_id=THREAD)
    async with async_run_with_context(req):
        out = await asyncio.wait_for(
            ask_user_handler(AskUserArgs(question="Which?"), ToolInvocationCtx(thread_id=THREAD)), timeout=2
        )
    assert json.loads(out)["reason"] == "no_one_watching"
