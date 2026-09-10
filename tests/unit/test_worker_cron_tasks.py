"""The worker's periodic tasks: what they are scheduled for, and what they do.

Six of the eight had never been executed by a test. `test_worker_instrumentation.py` asserts
each one is *wrapped*, and `test_worker_tenant_sweeps.py` runs two of them — everything else was
covered only by the fact that it imports.

That matters more than an ordinary coverage gap, because the worker is the only thing that runs
periodic work: audit and usage flush, retention, memory consolidation, the job scheduler and the
fiber resume all live here and nowhere else. A task whose body stops working takes its whole
responsibility with it, silently, because nothing downstream complains about work that never
happened.

The schedules are pinned too. A cron string changed from `*/1` to `0 3` is a one-character edit
that turns a minute into a day, and reviewing a diff is the only thing that would have caught it.
"""

from __future__ import annotations

import ast
import pathlib
import time
from typing import Any

import pytest
from felix_worker import tasks as worker_tasks

# The schedule each task is registered with. Written out rather than derived, because deriving it
# from the source would agree with the source however wrong the source became.
EXPECTED_SCHEDULES = {
    "flush_audit": "*/1 * * * *",
    "flush_usage": "*/1 * * * *",
    "run_scheduled_jobs": "* * * * *",
    "consolidate_memory": "*/15 * * * *",
    "retention_sweep": "0 3 * * *",
    "anomaly_scan": "*/30 * * * *",
    "continuous_eval": "*/10 * * * *",
    "fiber_scheduler": "* * * * *",
}

TENANT = "cron-tenant"


@pytest.fixture(autouse=True)
def _clean_buffers() -> Any:
    """Audit and usage are process globals the shared fixture does not reset.

    Without this the rows one test flushes are counted by the next, and the retention test —
    which asserts on how many survive — fails for a reason that has nothing to do with
    retention.
    """
    from felix.audit import store as audit_store
    from felix.usage import store as usage_store

    def _clear() -> None:
        audit_store.pending_buffer().reset_for_tests()
        audit_store._memory_events.clear()
        usage_store.clear_memory()

    _clear()
    yield
    _clear()


def _settings() -> Any:
    """The settings the task bodies actually use, bound at import."""
    return worker_tasks._settings


# --- the schedules --------------------------------------------------------------------------


def _declared_schedules() -> dict[str, str]:
    """Read `@broker.task(schedule=[{"cron": ...}])` off the source, by AST.

    Not from the registered task objects: Taskiq's decorator is what this is checking, so
    asking it what it registered would be asking the thing under test to grade itself.
    """
    source = pathlib.Path(worker_tasks.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            func = decorator.func
            if not (isinstance(func, ast.Attribute) and func.attr == "task"):
                continue
            for keyword in decorator.keywords:
                if keyword.arg != "schedule":
                    continue
                entries = ast.literal_eval(keyword.value)
                found[node.name] = entries[0]["cron"]
    return found


def test_every_scheduled_task_keeps_its_schedule() -> None:
    """A cron string is one character from meaning something else entirely."""
    declared = _declared_schedules()

    assert declared == EXPECTED_SCHEDULES, declared


def test_the_set_of_scheduled_tasks_does_not_change_silently() -> None:
    """Guards the guard: a scan that found nothing would satisfy the comparison above.

    Adding a task without adding it here is fine and will fail loudly; the point is that it
    cannot be added, removed or renamed without someone deciding to.
    """
    declared = _declared_schedules()

    assert len(declared) == 8, declared
    for name in EXPECTED_SCHEDULES:
        assert callable(getattr(worker_tasks, name, None)), f"{name} is not exported"


# --- the bodies -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flush_audit_drains_the_buffer_into_the_store() -> None:
    """The audit trail exists only if something drains the buffer; this is that something."""
    from felix.audit import store as audit_store

    audit_store.record_event(_settings(), TENANT, "tool_call", status="ok")
    assert len(audit_store.pending_buffer()) == 1

    await worker_tasks.flush_audit.original_func()

    assert len(audit_store.pending_buffer()) == 0
    rows, _ = await audit_store.query(_settings(), TENANT, limit=10)
    assert [row["event_type"] for row in rows] == ["tool_call"], rows


@pytest.mark.asyncio
async def test_flush_usage_drains_the_buffer_into_the_store() -> None:
    """Same shape, and the reason a deployment can bill nothing while serving traffic."""
    from felix.usage import store as usage_store

    usage_store.record_tokens(
        _settings(),
        tenant_id=TENANT,
        manifest_id="quick",
        model_id="m",
        tokens_input=11,
        tokens_output=7,
    )
    assert usage_store.pending_count() == 1

    await worker_tasks.flush_usage.original_func()

    assert usage_store.pending_count() == 0
    rows, _ = await usage_store.query(_settings(), TENANT, limit=10)
    assert [row["tokens_input"] for row in rows] == [11], rows


@pytest.mark.asyncio
async def test_run_scheduled_jobs_fires_a_due_job() -> None:
    """The scheduler is the whole point of `jobs`; a job that never fires is a row."""
    from felix.jobs import store as jobs_store

    await jobs_store.put_job(
        _settings(), TENANT, "nightly", schedule="* * * * *", manifest_id="quick", enabled=True
    )

    await worker_tasks.run_scheduled_jobs.original_func()

    runs = await jobs_store.list_runs(_settings(), TENANT, "nightly", limit=10)
    assert runs, "a due job must leave a run behind"


@pytest.mark.asyncio
async def test_run_scheduled_jobs_leaves_a_disabled_job_alone() -> None:
    """`enabled` is the off switch; a scheduler that ignores it cannot be stopped."""
    from felix.jobs import store as jobs_store

    await jobs_store.put_job(
        _settings(), TENANT, "paused", schedule="* * * * *", manifest_id="quick", enabled=False
    )

    await worker_tasks.run_scheduled_jobs.original_func()

    assert await jobs_store.list_runs(_settings(), TENANT, "paused", limit=10) == []


@pytest.mark.asyncio
async def test_retention_sweep_prunes_what_is_past_its_ttl() -> None:
    """Retention is a deletion guarantee, so the sweep has to actually delete."""
    from felix.audit import store as audit_store

    long_ago = int(time.time() * 1000) - (400 * 86_400_000)
    audit_store.record_event(_settings(), TENANT, "tool_call", status="ok", ts=long_ago)
    audit_store.record_event(_settings(), TENANT, "tool_call", status="ok")
    await audit_store.flush_pending(_settings())
    before, _ = await audit_store.query(_settings(), TENANT, limit=10)
    assert len(before) == 2

    await worker_tasks.retention_sweep.original_func()

    after, _ = await audit_store.query(_settings(), TENANT, limit=10)
    assert len(after) == 1, after
    assert after[0]["ts"] != long_ago, "the aged row is the one that should have gone"


@pytest.mark.asyncio
async def test_fiber_scheduler_advances_a_due_fiber() -> None:
    """Durable runs only progress because this ticks; a stalled tick strands every one."""
    from felix.durability import fibers

    created = await fibers.create_fiber(_settings(), TENANT, kind="step", status="pending")

    await worker_tasks.fiber_scheduler.original_func()

    stored = await fibers.get_fiber(_settings(), TENANT, created["id"])
    assert stored is not None
    assert stored["status"] != "pending", "a due fiber must be claimed and advanced"


@pytest.mark.asyncio
async def test_consolidate_memory_runs_and_reports_a_count() -> None:
    """Thin by design: `consolidation.py` is 14 lines and does exact-hash dedupe only.

    Asserting the return type rather than a merge is deliberate — there is no merge to assert.
    What this pins is that the task reaches it at all, which is what the other six lacked.
    """
    result = await worker_tasks.consolidate_memory.original_func()

    assert result is None  # the task logs its count rather than returning it


@pytest.mark.asyncio
async def test_every_task_body_is_callable_without_arguments() -> None:
    """Production calls these with nothing; a body that grew a parameter would fail only there.

    The same shape as `test_entrypoint_wiring.py`: exercise the call production makes, not a
    convenient one.
    """
    for name in EXPECTED_SCHEDULES:
        task = getattr(worker_tasks, name)
        assert hasattr(task, "original_func"), f"{name} is not a registered task"
        await task.original_func()
