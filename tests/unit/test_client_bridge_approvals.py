"""Client tool bridge and approval interrupt waiters."""

from __future__ import annotations

import asyncio

import pytest
from felix.approvals.interrupt import signal_decision, wait_for_decision
from felix.config import Settings
from felix.context import AuthContext, RequestContext, async_run_with_context
from felix.manifests.schema import ClientToolRef, Manifest
from felix.side_events import drain, emit
from felix.tools.client_bridge import complete_result, tools_from_client_refs, wait_for_result
from felix.tools.types import ToolInvocationCtx


@pytest.mark.asyncio
async def test_side_events_emit_drain() -> None:
    await emit("thread-a", "tool_request", {"id": "c1"})
    items = await drain("thread-a")
    assert items == [{"event": "tool_request", "data": {"id": "c1"}}]
    assert await drain("thread-a") == []


@pytest.mark.asyncio
async def test_client_bridge_complete() -> None:
    async def _waiter() -> str:
        result = await wait_for_result("default:t1", "call_1", timeout=2)
        return result.content

    task = asyncio.create_task(_waiter())
    await asyncio.sleep(0.05)
    ok = await complete_result("default:t1", "call_1", "pong")
    assert ok is True
    assert await task == "pong"


@pytest.mark.asyncio
async def test_client_tool_executor_roundtrip() -> None:
    tools = tools_from_client_refs([ClientToolRef(name="local_open", description="Open something")])
    tool = tools[0]
    assert tool.executor.transport == "client"

    async def _complete() -> None:
        await asyncio.sleep(0.05)
        await complete_result("default:t2", "call_open", '{"ok":true}')

    helper = asyncio.create_task(_complete())
    out = await tool.executor.execute(
        {"target": "file.txt"},
        ToolInvocationCtx(thread_id="default:t2", tool_call_id="call_open"),
    )
    await helper
    assert "ok" in str(out)


@pytest.mark.asyncio
async def test_approval_interrupt_signal() -> None:
    async def _wait() -> str:
        decision = await wait_for_decision("appr_1", timeout=2)
        return decision.decision

    task = asyncio.create_task(_wait())
    await asyncio.sleep(0.05)
    assert await signal_decision("appr_1", "approved", edited_args={"x": 1}) is True
    assert await task == "approved"


@pytest.mark.asyncio
async def test_cowork_manifest_loads() -> None:
    from felix.manifests.loader import load_bundled

    m = load_bundled("cowork")
    assert isinstance(m, Manifest)
    assert m.metadata.name == "cowork"
    assert "write_file" in m.spec.tools
    assert any(t.name == "local_shell" for t in m.spec.client_tools)
    assert m.spec.approvals
    assert m.spec.execution.mode == "durable"


@pytest.mark.asyncio
async def test_apply_approvals_waits_for_decide(tmp_path) -> None:
    from felix.manifests.builder import apply_approvals
    from felix.manifests.schema import ApprovalRule
    from felix.tools.types import define_tool

    async def _echo(args: dict) -> str:
        return f"echo:{args.get('value')}"

    tool = define_tool(name="write_file", description="w", handler=_echo)
    wrapped = apply_approvals(
        [tool],
        [ApprovalRule(id="w", tools=["write_file"], ttl_seconds=5)],
        "cowork",
    )[0]

    settings = Settings(allow_insecure=True, auth_mode="none", environment="development")
    req = RequestContext(
        settings=settings,
        auth=AuthContext(tenant_id="default"),
        manifest_id="cowork",
        thread_id="default:t3",
    )

    async def _decide_soon() -> None:
        from felix.approvals import store as approvals_store
        from felix.approvals.interrupt import signal_decision

        await asyncio.sleep(0.1)
        pending = await approvals_store.list_approvals(settings, "default", status="pending")
        assert pending, "expected pending approval"
        aid = pending[0]["id"]
        await approvals_store.decide(
            settings,
            "default",
            aid,
            decision="approved",
            decided_by="test",
            edited_args={"value": "ok"},
        )
        await signal_decision(aid, "approved", edited_args={"value": "ok"})

    helper = asyncio.create_task(_decide_soon())
    async with async_run_with_context(req):
        out = await wrapped.executor.execute(
            {"value": "nope"},
            ToolInvocationCtx(thread_id="default:t3", tool_call_id="c1"),
        )
    await helper
    assert str(out) == "echo:ok"


@pytest.mark.asyncio
async def test_approval_frame_names_the_rule_and_says_why() -> None:
    """`description` is the one field in `ApprovalRule` written to be read by a person.

    It used to reach no client by any route: the frame carried `rule_id` and nothing
    else, and the `/approvals` row does not carry the description either — so a banner
    asking an operator to authorize a write could only name `workspace-write`.
    """
    from felix.manifests.builder import apply_approvals
    from felix.manifests.schema import ApprovalRule
    from felix.tools.types import define_tool

    async def _echo(args: dict) -> str:
        return "written"

    tool = define_tool(name="write_file", description="w", handler=_echo)
    wrapped = apply_approvals(
        [tool],
        [
            ApprovalRule(
                id="workspace-write",
                description="Confirm writes to the workspace",
                tools=["write_file"],
                ttl_seconds=5,
            )
        ],
        "cowork",
    )[0]

    settings = Settings(allow_insecure=True, auth_mode="none", environment="development")
    req = RequestContext(
        settings=settings,
        auth=AuthContext(tenant_id="default"),
        manifest_id="cowork",
        thread_id="default:t4",
    )

    async def _deny_soon() -> None:
        from felix.approvals import store as approvals_store
        from felix.approvals.interrupt import signal_decision

        await asyncio.sleep(0.1)
        pending = await approvals_store.list_approvals(settings, "default", status="pending")
        assert pending, "expected pending approval"
        aid = pending[0]["id"]
        await approvals_store.decide(settings, "default", aid, decision="denied", decided_by="test")
        await signal_decision(aid, "denied")

    helper = asyncio.create_task(_deny_soon())
    async with async_run_with_context(req):
        await wrapped.executor.execute(
            {"path": "notes.txt"},
            ToolInvocationCtx(thread_id="default:t4", tool_call_id="c1"),
        )
    await helper

    frames = [f for f in await drain("default:t4") if f["event"] == "approval_required"]
    assert frames, "the gate emitted no approval_required"
    assert frames[0]["data"]["rule_id"] == "workspace-write"
    assert frames[0]["data"]["reason"] == "Confirm writes to the workspace"


@pytest.mark.asyncio
async def test_the_pending_row_names_the_thread_that_is_blocked() -> None:
    """The row, not only the frame — because for a durable run the row is all there is.

    `approval_required` has carried `thread_id` all along, but side events are an in-process
    queue keyed by thread: a durable run's agent is in the worker while its stream is served by
    the API, so no frame can cross and `GET /approvals` is the whole channel. It was the half
    with no thread on it, so an operator polling cold could be told that something was waiting
    but not what.

    The invocation ctx deliberately carries **no** thread, so only `req.thread_id` can supply
    one. That is not a convenience: `ToolInvocationCtx.thread_id` defaults to `None`, and on a
    durable run the thread is set by `durability/fibers.py` on the `RequestContext` it builds
    (`{tenant}:fiber:{id}`). With both halves set to the same value the fallback is invisible —
    dropping it from the production expression left this test green — and the invisible half is
    the one the docstring above is about.
    """
    from felix.approvals import store as approvals_store
    from felix.manifests.builder import apply_approvals
    from felix.manifests.schema import ApprovalRule
    from felix.tools.types import define_tool

    async def _echo(args: dict) -> str:
        return "written"

    wrapped = apply_approvals(
        [define_tool(name="write_file", description="w", handler=_echo)],
        [ApprovalRule(id="workspace-write", tools=["write_file"], ttl_seconds=5)],
        "cowork",
    )[0]

    settings = Settings(allow_insecure=True, auth_mode="none", environment="development")
    req = RequestContext(
        settings=settings,
        auth=AuthContext(tenant_id="default"),
        manifest_id="cowork",
        thread_id="default:t5",
    )
    seen: list[str] = []

    async def _deny_soon() -> None:
        from felix.approvals.interrupt import signal_decision

        await asyncio.sleep(0.1)
        pending = await approvals_store.list_approvals(settings, "default", status="pending")
        assert pending, "expected pending approval"
        # Read while the call is still blocked: this is exactly the poll a client makes.
        seen.append(pending[0]["thread_id"])
        await approvals_store.decide(settings, "default", pending[0]["id"], decision="denied", decided_by="t")
        await signal_decision(pending[0]["id"], "denied")

    helper = asyncio.create_task(_deny_soon())
    async with async_run_with_context(req):
        await wrapped.executor.execute(
            {"path": "notes.txt"},
            ToolInvocationCtx(tool_call_id="c1"),
        )
    await helper

    assert seen == ["default:t5"], "the poll path cannot say which conversation is waiting"
    frames = [f for f in await drain("default:t5") if f["event"] == "approval_required"]
    assert frames and frames[0]["data"]["thread_id"] == "default:t5", (
        "the two channels disagree about the thread"
    )


@pytest.mark.asyncio
async def test_a_command_screening_approval_names_its_thread_too() -> None:
    """The other `create_pending` call site: `require_approval` from command screening.

    It goes through `_await_approval` rather than `apply_approvals`, so it is a second place
    the thread could be dropped, and the one an operator hits by running a screened shell
    command rather than by calling a gated tool.

    The two sources disagree here on purpose — the sibling test above pins the `req.thread_id`
    fallback, this one pins that the invocation ctx takes precedence over it. Set to the same
    value, as they first were, neither half of `(ctx.thread_id if ctx else None) or
    req.thread_id` can be removed by a test.
    """
    from felix.approvals import store as approvals_store
    from felix.manifests.builder import apply_command_screening
    from felix.manifests.schema import CommandRule, CommandScreening
    from felix.tools.types import define_tool

    async def _run(args: dict) -> str:
        return "ran"

    wrapped = apply_command_screening(
        [define_tool(name="local_shell", description="s", handler=_run)],
        CommandScreening(
            enabled=True,
            include_defaults=False,
            target_tools=["local_shell"],
            approval_ttl_seconds=5,
            rules=[CommandRule(pattern="^git push", decision="require_approval", reason="pushes code")],
        ),
        "cowork",
    )[0]

    settings = Settings(allow_insecure=True, auth_mode="none", environment="development")
    req = RequestContext(
        settings=settings,
        auth=AuthContext(tenant_id="default"),
        manifest_id="cowork",
        thread_id="default:t6-request",
    )
    seen: list[str] = []

    async def _deny_soon() -> None:
        from felix.approvals.interrupt import signal_decision

        await asyncio.sleep(0.1)
        pending = await approvals_store.list_approvals(settings, "default", status="pending")
        assert pending, "expected pending approval"
        seen.append(pending[0]["thread_id"])
        await approvals_store.decide(settings, "default", pending[0]["id"], decision="denied", decided_by="t")
        await signal_decision(pending[0]["id"], "denied")

    helper = asyncio.create_task(_deny_soon())
    async with async_run_with_context(req):
        await wrapped.executor.execute(
            {"command": "git push --force"},
            ToolInvocationCtx(thread_id="default:t6-ctx", tool_call_id="c1"),
        )
    await helper

    assert seen == ["default:t6-ctx"], "the invocation ctx must win over the request context"
    # Mirrors the sibling test: the row and the frame must attribute the same thread, on this
    # path too. They are resolved once and shared, and this is what keeps that true.
    frames = [f for f in await drain("default:t6-ctx") if f["event"] == "approval_required"]
    assert frames and frames[0]["data"]["thread_id"] == "default:t6-ctx", (
        "the two channels disagree about the thread"
    )
