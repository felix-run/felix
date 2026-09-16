"""A durable run's approval row names the fiber's thread.

This is the case the `thread_id` column exists for, and the one every other test about it
infers rather than executes. `GET /approvals` is the **only** channel here by construction:
side events are an in-process queue keyed by thread, the fiber's agent runs in the worker,
and the stream is served by the API, so no `approval_required` frame can cross. If the row
carries no thread, an operator polling cold is told that something is waiting and not what.

`durability/fibers.py` synthesizes it — `thread = thread_id or f"{tenant_id}:fiber:{row['id']}"`
— and then feeds it to the step **twice**: onto the `RequestContext` and onto the `InvokeInput`,
which becomes `ToolInvocationCtx.thread_id`. The approval wrapper reads
`(ctx.thread_id if ctx else None) or req.thread_id`, so either one alone suffices. Measured by
mutation: dropping `thread_id` from the `RequestContext` leaves this green, dropping it from
the `InvokeInput` leaves this green, dropping both turns it red on `assert [''] == [...]`.

That is deliberately the granularity asserted. This test pins the *outcome* an operator sees —
a durable approval is attributable — not which of two redundant paths delivered it; a test that
failed when either was removed would be pinning the redundancy rather than the contract. The
sibling tests in `test_client_bridge_approvals.py` take the two halves apart individually.

It is driven end to end — real manifest store, real compile, real governance stack, real fiber
resume — with only the model scripted, because the defect shape here is a control that looks
present on a branch nothing executes.

It also pins the shape of the value, because a client links to it: `{tenant}:fiber:{id}` is a
real session thread, not a placeholder. A durable run started without a thread therefore never
reports the empty string, which is the one place the "empty where there is no thread" contract
in the changelog does not reach.
"""

from __future__ import annotations

import asyncio

import pytest
from felix.config import Settings
from felix.context import AuthContext, RequestContext, run_with_context
from felix.manifests.schema import ExecutionSpec
from felix.patterns.types import ChatMessage
from felix_ai.types import ToolCall

TENANT = "t"
ROUTE = "durable-scripted"


def _settings() -> Settings:
    return Settings(
        database_url="memory://ci",
        object_store="memory",
        auth_mode="none",
        allow_insecure=True,
        host="127.0.0.1",
        model_routes=f'{{"{ROUTE}":{{"provider":"scripted","model":"scripted-1"}}}}',
    )


@pytest.fixture
def scripted_calculator():
    """A model that calls the gated tool once, then answers. Unregistered afterwards, so a
    later test cannot route to a fake and pass on canned text."""
    from felix_ai import registry
    from felix_ai.providers.scripted import ScriptedTurn, register_scripted_provider

    # Snapshot and restore the whole dict, the way `tests/e2e/conftest.py` does: leaving
    # `scripted` registered would let a later test route to a fake and pass on canned text.
    saved = dict(registry._providers)
    register_scripted_provider(
        "scripted",
        [
            ScriptedTurn(
                content="",
                tool_calls=[ToolCall(id="call_1", name="calculator", args={"expression": "2+2"})],
                stop_reason="tool_use",
            ),
            ScriptedTurn(content="done"),
        ],
    )
    try:
        yield
    finally:
        registry._providers.clear()
        registry._providers.update(saved)


async def _gated_manifest(settings: Settings) -> None:
    from felix.manifests import store as manifest_store
    from felix.manifests.loader import parse_manifest

    manifest = parse_manifest(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": "gated"},
            "spec": {
                "pattern": "react",
                "model": {"id": ROUTE},
                "tools": ["calculator"],
                "execution": {"mode": "durable"},
                "approvals": [{"id": "math", "tools": ["calculator"], "ttl_seconds": 5}],
            },
        }
    )
    await manifest_store.put_version(settings, TENANT, "gated", manifest, created_by="test")
    await manifest_store.activate_version(settings, TENANT, "gated", version=1)


@pytest.mark.asyncio
async def test_a_durable_run_writes_its_fiber_thread_onto_the_approval(scripted_calculator) -> None:
    from felix.approvals import store as approvals_store
    from felix.approvals.interrupt import signal_decision
    from felix.durability.fibers import resume_due_fibers

    settings = _settings()
    await _gated_manifest(settings)

    ctx = RequestContext(
        settings=settings,
        auth=AuthContext(tenant_id=TENANT, principal_sub="alice", scheme="jwt", anonymous=False),
        manifest_id="gated",
    )
    # `thread_id=None` is the case that matters: the fiber has to synthesize one, which is
    # exactly the branch a caller who never opened a chat thread takes.
    with run_with_context(ctx):
        started = await start_durable_chat_compat(settings)

    fiber_id = started["resume_token"]
    seen: list[str] = []

    async def _decide_when_it_blocks() -> None:
        # Polls rather than sleeping a fixed time: the step has a manifest resolve and a full
        # governance compile in front of it, so a fixed sleep would be a race on a slow box.
        for _ in range(200):
            pending = await approvals_store.list_approvals(settings, TENANT, status="pending")
            if pending:
                seen.append(pending[0]["thread_id"])
                await approvals_store.decide(
                    settings, TENANT, pending[0]["id"], decision="denied", decided_by="operator"
                )
                await signal_decision(pending[0]["id"], "denied")
                return
            await asyncio.sleep(0.01)

    helper = asyncio.create_task(_decide_when_it_blocks())
    await resume_due_fibers(settings)
    await helper

    assert seen, "the durable run never created a pending approval, so this test proves nothing"
    assert seen == [f"{TENANT}:fiber:{fiber_id}"], (
        "a durable approval is unattributable: GET /approvals is its only channel"
    )


async def start_durable_chat_compat(settings: Settings) -> dict:
    from felix.durability.runs import start_durable_chat

    return await start_durable_chat(
        settings,
        TENANT,
        manifest_id="gated",
        messages=[ChatMessage(role="user", content="what is 2+2")],
        thread_id=None,
        model_id=None,
        execution=ExecutionSpec(mode="durable"),
    )
