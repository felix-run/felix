"""Protocol + durability smoke tests."""

from __future__ import annotations

import pytest
from felix.config import Settings
from felix_api.composition import compose

from tests.support.factories import make_settings


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.mark.asyncio
async def test_mcp_tools_list_and_call(settings: Settings) -> None:
    from felix.mcp.server import handle_rpc

    tools = compose(settings)
    listed = await handle_rpc(settings=settings, tools=tools, method="tools/list", params={}, rpc_id=1)
    names = {t["name"] for t in listed["result"]["tools"]}
    assert "calculator" in names

    called = await handle_rpc(
        settings=settings,
        tools=tools,
        method="tools/call",
        params={"name": "calculator", "arguments": {"expression": "3+4"}},
        rpc_id=2,
    )
    assert called["result"]["isError"] is False
    assert "7" in called["result"]["content"][0]["text"]


@pytest.mark.asyncio
async def test_fibers_resume(settings: Settings) -> None:
    from felix.durability.fibers import create_fiber, resume_due_fibers

    await create_fiber(settings, "default", wake_at=1, kind="sleep")
    n = await resume_due_fibers(settings)
    assert n >= 1


@pytest.mark.asyncio
async def test_memory_turn_versioning(settings: Settings) -> None:
    from felix.memory.store import consolidate_pools, list_active, put_memory

    # Its own tenant. The in-memory store is one dict per process and `list_active` filters
    # by tenant alone, so under `-n auto` any test sharing this worker that leaves an active
    # memory for "default" (an e2e boot runs as "default") made this count 2 -- a flake on
    # whichever unrelated PR drew that split (#385).
    tenant = "protocols-memory-versioning"
    a = await put_memory(settings, tenant, content="Felix is a harness", origin_seq=1)
    await put_memory(
        settings,
        tenant,
        content="Felix is a harness",
        origin_seq=2,
        supersedes_id=a["id"],
    )
    active = await list_active(settings, tenant)
    assert len(active) == 1
    n = await consolidate_pools(settings)
    assert n >= 0
