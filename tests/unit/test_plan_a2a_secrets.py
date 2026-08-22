"""Plan tools + secrets hydration + A2A persistence."""

from __future__ import annotations

import json

import pytest
from felix.a2a import tasks as task_store
from felix.config import Settings
from felix.context import AuthContext, RequestContext, async_run_with_context
from felix.durability.fibers import create_fiber, get_fiber, resume_due_fibers
from felix.patterns import _plan_tools
from felix.secrets import collected_secret_values, hydrate_secrets


@pytest.fixture
def memory_settings(tmp_path) -> Settings:
    return Settings(
        database_url="memory://polish",
        auth_mode="none",
        allow_insecure=True,
        object_store="memory",
        data_dir=str(tmp_path),
        secrets_backend="env",
    )


@pytest.mark.asyncio
async def test_plan_tools_persist(memory_settings: Settings) -> None:
    tools = {t.name: t for t in _plan_tools()}
    auth = AuthContext(tenant_id="t1", principal_sub="tester", anonymous=False)
    req = RequestContext(settings=memory_settings, auth=auth, manifest_id="deep", thread_id="th1")
    async with async_run_with_context(req):
        created = await tools["plan_create"].executor.execute(
            {"title": "Ship", "goal": "land feature", "plan_id": "p1"}
        )
        body = json.loads(created if isinstance(created, str) else created.content)
        assert body["id"] == "p1"
        assert body["plan"]["steps"][0]["status"] == "pending"

        updated = await tools["plan_update_step"].executor.execute(
            {"plan_id": "p1", "step_id": "1", "status": "done"}
        )
        up = json.loads(updated if isinstance(updated, str) else updated.content)
        assert up["plan"]["steps"][0]["status"] == "done"

        got = await tools["plan_get"].executor.execute({"plan_id": "p1"})
        g = json.loads(got if isinstance(got, str) else got.content)
        assert g["plan"]["title"] == "Ship"


@pytest.mark.asyncio
async def test_a2a_tasks_persist_memory(memory_settings: Settings) -> None:
    task_store.clear_tasks()
    await task_store.put_task(
        memory_settings,
        "default",
        {"id": "t1", "status": {"state": "working"}, "manifest": "quick", "artifacts": []},
    )
    row = await task_store.get_task(memory_settings, "default", "t1")
    assert row is not None
    assert row["status"]["state"] == "working"
    canceled = await task_store.cancel_task(memory_settings, "default", "t1")
    assert canceled is not None
    assert canceled["status"]["state"] == "canceled"


@pytest.mark.asyncio
async def test_fiber_get_after_stash(memory_settings: Settings) -> None:
    fiber = await create_fiber(
        memory_settings,
        "default",
        status="running",
        state={
            "steps": [
                {"op": "stash", "data": {"prompt": "hi"}},
                {"op": "complete", "result": "done"},
            ],
            "cursor": 0,
            "stash": {},
        },
    )
    await resume_due_fibers(memory_settings)
    row = await get_fiber(memory_settings, "default", fiber["id"])
    assert row is not None
    assert row["state_json"]["stash"].get("prompt") == "hi"


@pytest.mark.asyncio
async def test_hydrate_secrets_from_env(memory_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-secret-value")
    memory_settings.anthropic_api_key = ""
    found = await hydrate_secrets(memory_settings)
    assert memory_settings.anthropic_api_key == "sk-ant-test-secret-value"
    assert "sk-ant-test-secret-value" in found
    assert "sk-ant-test-secret-value" in collected_secret_values(memory_settings)
