"""In-memory control-plane store tests (memory:// database_url)."""

from __future__ import annotations

import pytest
from felix.approvals import store as approvals_store
from felix.audit import store as audit_store
from felix.config import Settings
from felix.eval import store as eval_store
from felix.eval.runner import start_run
from felix.jobs import store as jobs_store
from felix.manifests import store as manifest_store
from felix.manifests.loader import parse_manifest
from felix.manifests.store import PostgresManifestStore
from felix.plans import store as plans_store
from felix.session.store import append_event, get_session_store


@pytest.fixture
def memory_settings() -> Settings:
    return Settings(database_url="memory://test")


MINIMAL_MANIFEST = {
    "apiVersion": "felix/v1",
    "kind": "Agent",
    "metadata": {"name": "quick"},
    "spec": {},
}


@pytest.mark.asyncio
async def test_manifest_store_roundtrip(memory_settings: Settings) -> None:
    manifest = parse_manifest(MINIMAL_MANIFEST)
    created = await manifest_store.put_version(memory_settings, "t1", "quick", manifest, created_by="tester")
    assert created["version"] == 1
    assert created["manifest"]["metadata"]["name"] == "quick"

    active = await manifest_store.list_active(memory_settings, "t1")
    assert len(active) == 1
    assert active[0]["version"] == 1

    fetched = await manifest_store.get_version(memory_settings, "t1", "quick", 1)
    assert fetched is not None
    assert fetched["manifest"]["metadata"]["name"] == "quick"

    pg_store = PostgresManifestStore(memory_settings)
    pointer = await pg_store.get_active("t1", "quick")
    assert pointer is not None
    assert pointer.version == 1
    resolved = await pg_store.get_version("t1", "quick", 1)
    assert resolved is not None
    assert resolved.metadata.name == "quick"


@pytest.mark.asyncio
async def test_audit_buffer_and_flush(memory_settings: Settings) -> None:
    audit_store._pending.clear()
    audit_store._memory_events.clear()
    audit_store.record_event(
        memory_settings,
        "t1",
        "tool_call",
        manifest_id="quick",
        status="ok",
        payload_json={"tool": "search"},
    )
    assert await audit_store.flush_pending(memory_settings) == 1

    items, cursor = await audit_store.list_events(memory_settings, "t1", limit=10)
    assert len(items) == 1
    assert items[0]["event_type"] == "tool_call"
    assert cursor is None


@pytest.mark.asyncio
async def test_approvals_crud(memory_settings: Settings) -> None:
    pending = await approvals_store.create_pending(
        memory_settings,
        "t1",
        tool_name="run_cmd",
        call_signature="sig1",
        args={"cmd": "echo hi"},
    )
    assert pending["status"] == "pending"

    listed = await approvals_store.list_approvals(memory_settings, "t1")
    assert len(listed) == 1

    decided = await approvals_store.decide(
        memory_settings,
        "t1",
        pending["id"],
        decision="approved",
        decided_by="admin",
    )
    assert decided is not None
    assert decided["status"] == "approved"


@pytest.mark.asyncio
async def test_plans_and_jobs(memory_settings: Settings) -> None:
    plan = await plans_store.put_plan(
        memory_settings,
        "t1",
        "plan-1",
        plan={"steps": [{"id": "s1"}]},
        manifest_id="quick",
    )
    assert plan["id"] == "plan-1"

    jobs = await jobs_store.put_job(
        memory_settings,
        "t1",
        "nightly",
        schedule="0 0 * * *",
        payload={"topic": "sync"},
    )
    assert jobs["name"] == "nightly"
    assert jobs["payload"]["topic"] == "sync"


@pytest.mark.asyncio
async def test_eval_run_completes_without_items(memory_settings: Settings) -> None:
    await eval_store.put_dataset(memory_settings, "t1", "smoke", items=[])
    result = await start_run(
        memory_settings,
        tenant_id="t1",
        dataset_name="smoke",
        candidate_manifest="quick",
    )
    assert result["status"] == "completed"
    assert result["scores"] == []


@pytest.mark.asyncio
async def test_session_append_event(memory_settings: Settings) -> None:
    event = await append_event(
        settings=memory_settings,
        tenant_id="t1",
        session_id="thread-1",
        event_type="message",
        payload={"role": "user", "content": "hello"},
    )
    assert event["type"] == "message"

    store = get_session_store(memory_settings, tenant_id="t1")
    session = store.open("thread-1")
    events = await session.get_events()
    assert len(events) == 1
    assert events[0].content == "hello"
