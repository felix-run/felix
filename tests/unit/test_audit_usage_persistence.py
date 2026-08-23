"""Audit and usage events must actually reach Postgres from the process that emits them.

`emit_agent_audit` / `record_usage` are called from the agent loop, which runs in the
**API** process, but `flush_pending` used to be called only from Taskiq cron tasks in the
**worker**. Where those are separate containers (Compose, Helm) the worker's buffer was
always empty and the API's was never drained: the audit trail was silently empty and the
buffer leaked for the life of the process.
"""

from __future__ import annotations

import asyncio

import pytest
from felix.audit import store as audit_store
from felix.buffers import DurableBuffer
from felix.config import Settings
from felix.flush import flush_all, start_flush_task, stop_flush_task
from felix.usage import store as usage_store


def _settings(**kw: object) -> Settings:
    base: dict[str, object] = {
        "database_url": "memory://flush",
        "object_store": "memory",
        "allow_insecure": True,
        "auth_mode": "none",
        "environment": "development",
    }
    base.update(kw)
    return Settings(**base)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _clean() -> None:
    audit_store._pending.reset_for_tests()
    audit_store._memory_events.clear()
    usage_store.clear_memory()


# --- the buffer contract --------------------------------------------------------


def test_failed_write_does_not_lose_the_batch() -> None:
    buf = DurableBuffer("t")
    buf.append({"id": "a"})
    buf.append({"id": "b"})
    batch = buf.take()
    assert len(buf) == 0
    buf.requeue(batch)
    assert [e["id"] for e in buf.snapshot()] == ["a", "b"]


def test_requeue_preserves_order_ahead_of_newer_events() -> None:
    buf = DurableBuffer("t")
    batch = [{"id": "old1"}, {"id": "old2"}]
    buf.append({"id": "new"})
    buf.requeue(batch)
    assert [e["id"] for e in buf.snapshot()] == ["old1", "old2", "new"]


def test_buffer_is_bounded_and_counts_drops() -> None:
    buf = DurableBuffer("t", max_pending=3)
    for i in range(5):
        buf.append({"id": i})
    assert len(buf) == 3
    assert buf.dropped == 2
    # oldest dropped, newest kept
    assert [e["id"] for e in buf.snapshot()] == [2, 3, 4]


def test_requeue_respects_the_ceiling() -> None:
    """A permanently-failing database must not pin memory forever."""
    buf = DurableBuffer("t", max_pending=2)
    buf.requeue([{"id": i} for i in range(10)])
    assert len(buf) == 2
    assert buf.dropped == 8


@pytest.mark.asyncio
async def test_flush_requeues_on_write_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    audit_store.record_event(_settings(), "t1", "tool_call", manifest_id="m")
    assert len(audit_store._pending) == 1

    async def _boom(settings, batch):
        raise RuntimeError("commit failed")

    monkeypatch.setattr(audit_store, "_write_batch", _boom)
    with pytest.raises(RuntimeError):
        await audit_store.flush_pending(_settings())
    # Pre-fix this batch was cleared before the write and lost forever.
    assert len(audit_store._pending) == 1


# --- the API process actually flushes -------------------------------------------


@pytest.mark.asyncio
async def test_flush_all_drains_both_stores() -> None:
    s = _settings()
    audit_store.record_event(s, "t1", "tool_call", manifest_id="m")
    usage_store.record_tokens(
        s, tenant_id="t1", manifest_id="m", model_id="x", tokens_input=5, tokens_output=7
    )
    audit_n, usage_n = await flush_all(s)
    assert audit_n == 1
    assert usage_n == 1
    assert len(audit_store._pending) == 0


@pytest.mark.asyncio
async def test_flush_loop_drains_without_the_worker() -> None:
    """This is the regression: no worker process involved anywhere."""
    s = _settings(audit_flush_seconds=0.05)
    task = start_flush_task(s)
    assert task is not None
    try:
        audit_store.record_event(s, "t1", "policy_deny", manifest_id="m")
        for _ in range(40):
            await asyncio.sleep(0.05)
            if not len(audit_store._pending):
                break
        assert len(audit_store._pending) == 0
        assert len(audit_store._memory_events) == 1
    finally:
        await stop_flush_task(task, s)


@pytest.mark.asyncio
async def test_shutdown_drains_remaining_events() -> None:
    s = _settings(audit_flush_seconds=3600)  # never fires on its own
    task = start_flush_task(s)
    audit_store.record_event(s, "t1", "user_input", manifest_id="m")
    await stop_flush_task(task, s)
    assert len(audit_store._pending) == 0
    assert len(audit_store._memory_events) == 1


def test_flush_task_disabled_when_interval_zero() -> None:
    assert start_flush_task(_settings(audit_flush_seconds=0)) is None


@pytest.mark.asyncio
async def test_flush_all_isolates_store_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    """An audit failure must not strand usage (and vice versa)."""
    s = _settings()

    async def _boom(settings):
        raise RuntimeError("audit down")

    monkeypatch.setattr(audit_store, "flush_pending", _boom)
    usage_store.record_tokens(
        s, tenant_id="t1", manifest_id="m", model_id="x", tokens_input=1, tokens_output=1
    )
    audit_n, usage_n = await flush_all(s)
    assert audit_n == 0
    assert usage_n == 1


# --- end to end: the API app wires the flusher into its lifespan ----------------


@pytest.mark.asyncio
async def test_api_lifespan_flushes_audit_without_a_worker() -> None:
    """Pre-fix, an event emitted while serving traffic was never written anywhere."""
    from felix_api.app import create_app

    cfg = _settings(host="127.0.0.1", audit_flush_seconds=0.05)
    app = create_app(settings=cfg, plugins=[])

    async with app.router.lifespan_context(app):
        assert app.state.flush_task is not None, "API process must run its own flusher"
        audit_store.record_event(cfg, "t1", "tool_call", manifest_id="m")
        for _ in range(40):
            await asyncio.sleep(0.05)
            if not len(audit_store._pending):
                break
        assert len(audit_store._memory_events) == 1

    # and shutdown drains anything still buffered
    assert len(audit_store._pending) == 0
