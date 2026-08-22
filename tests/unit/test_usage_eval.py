"""Usage meters + mock eval fixture."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from felix.config import Settings
from felix.patterns.model import ModelChatResult, TokenUsage, record_usage
from felix.patterns.types import ChatMessage


@pytest.fixture
def memory_settings() -> Settings:
    return Settings(
        allow_insecure=True,
        auth_mode="none",
        environment="development",
        database_url="memory://test",
        object_store="memory",
    )


@pytest.mark.asyncio
async def test_usage_record_and_flush(memory_settings: Settings) -> None:
    from felix.usage import store as usage_store

    usage_store.clear_memory()
    record_usage(
        ModelChatResult(
            message=ChatMessage(role="assistant", content="hi"),
            usage=TokenUsage(input=3, output=5, cache_creation=1, cache_read=2),
        ),
        manifest_id="quick",
        model_id="haiku",
    )
    assert usage_store.pending_count() == 1
    n = await usage_store.flush_pending(memory_settings)
    assert n == 1
    items, _ = await usage_store.query(memory_settings, "default")
    assert len(items) == 1
    assert items[0]["tokens_input"] == 3
    assert items[0]["tokens_output"] == 5
    assert items[0]["manifest_id"] == "quick"


@pytest.mark.asyncio
async def test_mock_eval_fixture(memory_settings: Settings) -> None:
    from felix.eval import store as eval_store
    from felix.eval.runner import start_run

    fixture = Path(__file__).resolve().parents[2] / "fixtures" / "eval" / "smoke.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    await eval_store.put_dataset(
        memory_settings,
        "default",
        payload["name"],
        description=payload.get("description", ""),
        items=payload["items"],
    )
    result = await start_run(
        memory_settings,
        tenant_id="default",
        dataset_name="smoke",
        candidate_manifest="quick",
        mock=True,
    )
    assert result["pass_count"] == 3
    assert result["fail_count"] == 0
