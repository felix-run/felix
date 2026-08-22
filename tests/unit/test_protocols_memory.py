"""Protocol + durability smoke tests."""

from __future__ import annotations

import pytest
from felix.config import Settings
from felix_api.composition import compose


@pytest.fixture
def settings() -> Settings:
    return Settings(
        auth_mode="none",
        allow_insecure=True,
        object_store="memory",
        database_url="memory://test",
    )


@pytest.mark.asyncio
async def test_mcp_tools_list_and_call(settings: Settings) -> None:
    from felix.mcp.server import handle_rpc

    tools = compose(settings)
    listed = await handle_rpc(
        settings=settings, tools=tools, method="tools/list", params={}, rpc_id=1
    )
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

    a = await put_memory(settings, "default", content="Felix is a harness", origin_seq=1)
    await put_memory(
        settings,
        "default",
        content="Felix is a harness",
        origin_seq=2,
        supersedes_id=a["id"],
    )
    active = await list_active(settings, "default")
    assert len(active) == 1
    n = await consolidate_pools(settings)
    assert n >= 0
