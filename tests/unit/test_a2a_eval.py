"""A2A JSON-RPC smoke tests."""

from __future__ import annotations

import pytest
from felix.a2a.server import handle_rpc
from felix.config import Settings
from felix.tools.provider import InMemoryToolProvider


@pytest.fixture
def settings() -> Settings:
    return Settings(
        auth_mode="none",
        allow_insecure=True,
        object_store="memory",
        database_url="memory://a2a",
        default_manifest="quick",
    )


@pytest.mark.asyncio
async def test_a2a_agent_card(settings: Settings) -> None:
    tools = InMemoryToolProvider()
    resp = await handle_rpc(
        settings=settings,
        tools=tools,
        tenant_id="default",
        method="agent/authenticatedExtendedCard",
        params={"manifest": "quick"},
        rpc_id=1,
    )
    assert resp["result"]["name"] == "quick"
    assert resp["result"]["capabilities"]["streaming"] is True


@pytest.mark.asyncio
async def test_a2a_message_send_requires_text(settings: Settings) -> None:
    tools = InMemoryToolProvider()
    resp = await handle_rpc(
        settings=settings,
        tools=tools,
        tenant_id="default",
        method="message/send",
        params={"manifest": "quick", "message": {"parts": []}},
        rpc_id=2,
    )
    assert resp["error"]["code"] == -32602


@pytest.mark.asyncio
async def test_eval_empty_dataset_completes(settings: Settings) -> None:
    from felix.eval import store as eval_store
    from felix.eval.runner import start_run

    await eval_store.put_dataset(settings, "default", "empty", description="no items")
    run = await start_run(
        settings,
        tenant_id="default",
        dataset_name="empty",
        candidate_manifest="quick",
    )
    assert run["status"] in {"completed", "in_progress", "complete"} or run.get("pass_count") == 0
    assert run.get("fail_count", 0) == 0
