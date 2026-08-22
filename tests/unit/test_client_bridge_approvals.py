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
    tools = tools_from_client_refs(
        [ClientToolRef(name="local_open", description="Open something")]
    )
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
