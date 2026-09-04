"""The worker's periodic sweeps must be visible to a scrape, not only to a log reader.

Before this, every task in `felix_worker.tasks` reported one `logger.info` line and
nothing else. That is the failure mode `deploy/GOVERNANCE.md` calls out for the per-tenant
detection controls: a sweep that has stopped firing looks exactly like a sweep that ran
and found nothing. Neither a counter nor a span existed for any of them, and
`setup_observability` was never called in the worker process at all, so even the two spans
that did exist were never exported from the process that owns fibers and retention.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest
from felix.observability import metrics as metrics_mod

TASKS_FILE = Path(__file__).resolve().parents[2] / "apps/worker/src/felix_worker/tasks.py"

# Every task Taskiq fires on a schedule. Named explicitly so adding a sweep without
# instrumenting it is a failure rather than a silent omission.
SCHEDULED_TASKS = {
    "flush_audit",
    "flush_usage",
    "run_scheduled_jobs",
    "consolidate_memory",
    "retention_sweep",
    "anomaly_scan",
    "continuous_eval",
    "fiber_scheduler",
}


def _decorator_names(fn: ast.AsyncFunctionDef) -> set[str]:
    names: set[str] = set()
    for dec in fn.decorator_list:
        node = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return names


def test_every_scheduled_task_is_instrumented() -> None:
    tree = ast.parse(TASKS_FILE.read_text())
    scheduled: dict[str, set[str]] = {
        node.name: _decorator_names(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and "task" in _decorator_names(node)
    }
    assert set(scheduled) == SCHEDULED_TASKS, (
        "the set of scheduled worker tasks changed; instrument the new one and update this list"
    )
    uninstrumented = sorted(name for name, decs in scheduled.items() if "_instrumented" not in decs)
    assert not uninstrumented, f"scheduled tasks with no counter or span: {uninstrumented}"


def test_the_worker_configures_logging_and_tracing_at_startup() -> None:
    """The API did both; the worker did neither, so worker spans never left the process."""
    source = TASKS_FILE.read_text()
    for call in ("configure_logging(_settings)", "setup_observability(_settings)"):
        assert call in source, f"worker startup does not call {call}"
    assert "shutdown_observability()" in source, "the last span batch would be lost on shutdown"


@pytest.mark.asyncio
async def test_a_sweep_records_a_counter_and_a_duration() -> None:
    import felix_worker.tasks as tasks

    seen: list[tuple[str, Any]] = []
    counter, histogram = metrics_mod.record_counter, metrics_mod.record_histogram
    tasks.record_counter = lambda name, labels=None, value=1: seen.append((name, labels))  # type: ignore[assignment]
    tasks.record_histogram = lambda name, value, labels=None: seen.append((name, labels))  # type: ignore[assignment]
    try:

        @tasks._instrumented("demo_sweep")
        async def _sweep() -> None:
            return None

        await _sweep()
    finally:
        tasks.record_counter, tasks.record_histogram = counter, histogram  # type: ignore[assignment]

    assert ("felix_worker_task", {"task": "demo_sweep", "status": "ok"}) in seen
    assert ("felix_worker_task_seconds", {"task": "demo_sweep"}) in seen


@pytest.mark.asyncio
async def test_a_failing_sweep_is_counted_as_error_and_still_raises() -> None:
    """Swallowing the exception would make a permanently broken sweep look healthy."""
    import felix_worker.tasks as tasks

    seen: list[tuple[str, Any]] = []
    counter, histogram = metrics_mod.record_counter, metrics_mod.record_histogram
    tasks.record_counter = lambda name, labels=None, value=1: seen.append((name, labels))  # type: ignore[assignment]
    tasks.record_histogram = lambda name, value, labels=None: None  # type: ignore[assignment]
    try:

        @tasks._instrumented("broken_sweep")
        async def _sweep() -> None:
            raise RuntimeError("postgres is gone")

        with pytest.raises(RuntimeError, match="postgres is gone"):
            await _sweep()
    finally:
        tasks.record_counter, tasks.record_histogram = counter, histogram  # type: ignore[assignment]

    assert ("felix_worker_task", {"task": "broken_sweep", "status": "error"}) in seen


def test_the_metrics_server_stays_off_by_default() -> None:
    """It is unauthenticated and carries tenant-supplied manifest ids in its labels."""
    import felix_worker.tasks as tasks

    started: list[int] = []
    original = tasks._settings.metrics_port
    try:
        object.__setattr__(tasks._settings, "metrics_port", 0)
        import prometheus_client

        real = prometheus_client.start_http_server
        prometheus_client.start_http_server = lambda port, *a, **k: started.append(port)  # type: ignore[assignment]
        try:
            tasks._start_metrics_server()
        finally:
            prometheus_client.start_http_server = real  # type: ignore[assignment]
    finally:
        object.__setattr__(tasks._settings, "metrics_port", original)
    assert started == [], "the worker opened an unauthenticated metrics port with FELIX_METRICS_PORT=0"
