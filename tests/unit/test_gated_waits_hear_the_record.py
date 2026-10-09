"""A run waiting on a person or a client hears what the signal cannot carry (felix-run/felix#532).

An approval wait and a client tool's wait both listened for one signal and nothing else. So a
decision whose signal was lost between the API and the worker was a timeout to the run, after a
person had clicked Approve; and a Stop on the thread left the call waiting out its whole deadline,
because the abort flag is read only between tool rounds. A client tool that reported a failure
reached the model as plain text, its `error` flag dropped on the way.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from felix.approvals import interrupt
from felix.approvals import store as approvals
from felix.config import Settings
from felix.manifests.builder import _decision_check
from felix.steer import clear_abort, request_abort
from felix.tools import client_bridge
from felix.tools.errors import read_tool_error_code
from felix.tools.types import ToolInvocationCtx, tool_output_content

THREAD = "default:gated-wait"


def _settings() -> Settings:
    return Settings(database_url="memory://gated-waits", object_store="memory", redis_url="")


def _req(settings: Settings) -> Any:
    return SimpleNamespace(settings=settings, auth=SimpleNamespace(tenant_id="default"))


async def _row(settings: Settings) -> dict[str, Any]:
    return await approvals.create_pending(
        settings,
        "default",
        tool_name="local_write",
        call_signature="sig",
        manifest_id="cowork",
        args={"path": "a.md"},
        thread_id=THREAD,
        tool_call_id="call_w",
    )


@pytest.fixture(autouse=True)
def _fast_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(interrupt, "CHECK_SECONDS", 0.02)
    monkeypatch.setattr(client_bridge, "ABORT_CHECK_SECONDS", 0.02)


@pytest.mark.asyncio
async def test_a_decision_whose_signal_never_arrives_is_read_from_the_row() -> None:
    settings = _settings()
    row = await _row(settings)
    waiting = asyncio.create_task(
        interrupt.wait_for_decision(
            row["id"], timeout=5, check=_decision_check(_req(settings), row["id"], THREAD)
        )
    )
    await asyncio.sleep(0.05)
    # Written the way `/approvals/{id}/decide` writes it -- and the signal lost.
    await approvals.decide(
        settings, "default", row["id"], decision="approved", decided_by="op", edited_args={"path": "b.md"}
    )

    decision = await asyncio.wait_for(waiting, timeout=5)
    assert (decision.decision, decision.edited_args) == ("approved", {"path": "b.md"})


@pytest.mark.asyncio
async def test_a_stop_ends_an_approval_wait_instead_of_its_deadline() -> None:
    settings = _settings()
    row = await _row(settings)
    try:
        waiting = asyncio.create_task(
            interrupt.wait_for_decision(
                row["id"], timeout=600, check=_decision_check(_req(settings), row["id"], THREAD)
            )
        )
        await asyncio.sleep(0.05)
        await request_abort("default", THREAD)
        decision = await asyncio.wait_for(waiting, timeout=5)
    finally:
        await clear_abort("default", THREAD)
    assert (decision.decision, decision.note) == ("denied", "aborted")


@pytest.mark.asyncio
async def test_a_stop_ends_a_client_tools_wait_instead_of_its_timeout() -> None:
    try:
        waiting = asyncio.create_task(
            client_bridge.wait_for_result(THREAD, "call_c", timeout=600, tenant_id="default")
        )
        await asyncio.sleep(0.05)
        await request_abort("default", THREAD)
        result = await asyncio.wait_for(waiting, timeout=5)
    finally:
        await clear_abort("default", THREAD)
    assert result.error and "user_aborted" in result.content


@pytest.mark.asyncio
async def test_a_failure_the_client_reports_reaches_the_run_as_a_failure() -> None:
    from felix.manifests.schema import ClientToolRef

    (tool,) = client_bridge.tools_from_client_refs(
        [ClientToolRef(name="local_write", description="w", timeout_seconds=5)]
    )
    ctx = ToolInvocationCtx(thread_id=THREAD, tool_call_id="call_fail")
    running = asyncio.create_task(tool.executor.execute({"path": "a.md"}, ctx))
    await asyncio.sleep(0.05)
    await client_bridge.complete_result(THREAD, "call_fail", "EACCES: permission denied", error=True)

    out = await asyncio.wait_for(running, timeout=5)
    assert read_tool_error_code(out) is not None, "a reported failure arrived as a success"
    assert "EACCES" in tool_output_content(out)
