"""Fiber step runner + A2A task store."""

from __future__ import annotations

import pytest

from felix.a2a import tasks as task_store
from felix.config import Settings
from felix.durability.fibers import create_fiber, resume_due_fibers


@pytest.fixture
def settings() -> Settings:
    return Settings(
        auth_mode="none",
        allow_insecure=True,
        object_store="memory",
        database_url="memory://fiber",
    )


@pytest.mark.asyncio
async def test_fiber_stash_then_complete(settings: Settings) -> None:
    await create_fiber(
        settings,
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
    n = await resume_due_fibers(settings)
    assert n >= 1
    n2 = await resume_due_fibers(settings)
    assert n2 >= 1


@pytest.mark.asyncio
async def test_fiber_sleep_wake(settings: Settings) -> None:
    await create_fiber(
        settings,
        "default",
        status="running",
        state={
            "steps": [
                {"op": "sleep", "delay_ms": 0},
                {"op": "complete"},
            ],
            "cursor": 0,
        },
    )
    await resume_due_fibers(settings)
    n = await resume_due_fibers(settings)
    assert n >= 1


@pytest.mark.asyncio
async def test_a2a_task_store_roundtrip(settings: Settings) -> None:
    task_store.clear_tasks()
    await task_store.put_task(
        settings,
        "default",
        {"id": "t1", "status": {"state": "working"}, "artifacts": []},
    )
    got = await task_store.get_task(settings, "default", "t1")
    assert got is not None
    assert got["status"]["state"] == "working"
    canceled = await task_store.cancel_task(settings, "default", "t1")
    assert canceled is not None
    assert canceled["status"]["state"] == "canceled"
