"""Felix worker tasks — audit, cron, memory, retention, eval, fibers."""

from __future__ import annotations

import functools
import logging
from collections.abc import Awaitable, Callable

from felix.config import get_settings
from felix.observability.tracing import (
    setup_log_export,
    setup_observability,
    shutdown_observability,
    timed_span,
)
from taskiq import TaskiqEvents, TaskiqScheduler
from taskiq.schedule_sources import LabelScheduleSource
from taskiq_redis import ListQueueBroker, RedisAsyncResultBackend

logger = logging.getLogger("felix.worker")

_settings = get_settings()
# redis-py 8 defaults socket_timeout=5s; ListQueueBroker BRPOP blocks forever
# (taskiq-redis#127). Disable read timeout so idle workers stay alive.
# Explicit kwargs (not **dict) keep ty from mis-matching redis-py option types.
broker = ListQueueBroker(
    url=_settings.redis_url,
    socket_timeout=None,
    socket_connect_timeout=5.0,
).with_result_backend(
    RedisAsyncResultBackend(
        redis_url=_settings.redis_url,
        socket_timeout=None,
        socket_connect_timeout=5.0,
    )
)
scheduler = TaskiqScheduler(broker=broker, sources=[LabelScheduleSource(broker)])


@broker.on_event(TaskiqEvents.WORKER_STARTUP)
async def _on_worker_startup(_state: object) -> None:
    from felix.logging_setup import configure_logging
    from felix.plugins import get_registry, load_optional_plugins
    from felix.secrets import hydrate_secrets

    # The worker used to configure none of this. Logs came out at Taskiq's level with no
    # request id and no JSON in production, and `setup_observability` was only ever called
    # by the API — so every fiber resume, flush and sweep below exported no span at all.
    configure_logging(_settings)
    setup_observability(_settings)
    setup_log_export(_settings)
    _start_metrics_server()

    load_optional_plugins()
    # Backend names are open strings resolved against their registries, so validate
    # here too. The API validated at create_app; without this the worker learned
    # about FELIX_SECRETS_BACKEND=vualt from a traceback in the middle of a task.
    _settings.validate_runtime()
    await hydrate_secrets(_settings)
    _register_plugin_cron_tasks()
    logger.info(
        "worker_startup secrets_backend=%s plugins=%s",
        _settings.secrets_backend,
        len(get_registry().plugins),
    )


@broker.on_event(TaskiqEvents.WORKER_SHUTDOWN)
async def _on_worker_shutdown(_state: object) -> None:
    # Without this the BatchSpanProcessor's queue dies with the process and the last
    # batch of spans is simply lost.
    shutdown_observability()


def _start_metrics_server() -> None:
    """Expose the worker's Prometheus counters when FELIX_METRICS_PORT is set.

    The worker has no HTTP server of its own, so its counters — fiber steps, flush
    volumes, every cron sweep — were unreachable by any scrape. This endpoint carries no
    authentication (unlike the API's `/metrics`, which is auth-gated because its labels
    include tenant-supplied manifest ids); the same labels appear here, so bind it to an
    internal network and never publish the port.
    """
    port = int(getattr(_settings, "metrics_port", 0) or 0)
    if port <= 0:
        return
    try:
        from prometheus_client import start_http_server

        start_http_server(port)
    except Exception:
        logger.warning("metrics server failed to start on port %s", port, exc_info=True)
        return
    logger.info("worker_metrics port=%s", port)


def _instrumented(task_name: str) -> Callable[[Callable[[], Awaitable[None]]], Callable[[], Awaitable[None]]]:
    """Count, time and trace one periodic sweep.

    Every task below reported a log line and nothing else, so a sweep that had stopped
    firing was indistinguishable from one that ran and found nothing — which is exactly
    the confusion `deploy/GOVERNANCE.md` warns about for the per-tenant detection controls.
    """

    def decorate(fn: Callable[[], Awaitable[None]]) -> Callable[[], Awaitable[None]]:
        @functools.wraps(fn)
        async def wrapped() -> None:
            async with timed_span(
                f"worker {task_name}",
                {"felix.worker.task": task_name},
                metric="felix_worker_task_seconds",
                counter="felix_worker_task",
                labels={"task": task_name},
            ):
                await fn()

        return wrapped

    return decorate


def _register_plugin_cron_tasks() -> None:
    """Register optional plugin cron runners as Taskiq tasks (best-effort).

    Plugins that expose ``cron_tasks`` get a Taskiq task named
    ``plugin_<name>`` scheduled every minute. The plugin ``run`` coroutine
    is invoked with no args; richer schedules can be added later.
    """
    from felix.plugins import get_registry

    for plugin in get_registry().plugins:
        cron_tasks = getattr(plugin, "cron_tasks", ()) or ()
        for task in cron_tasks:
            name = f"plugin_{getattr(task, 'name', 'unnamed')}"
            run = getattr(task, "run", None)
            if not callable(run):
                continue
            if name in broker.local_task_registry:
                continue

            async def _runner(_run=run, _name=name) -> None:
                await _run()
                logger.info("plugin_cron name=%s", _name)

            broker.register_task(
                _runner,
                task_name=name,
                schedule=[{"cron": "* * * * *"}],
            )
            logger.info("plugin_cron_registered name=%s", name)


@broker.task(schedule=[{"cron": "*/1 * * * *"}])
@_instrumented("flush_audit")
async def flush_audit() -> None:
    """Drain buffered audit events to Postgres."""
    from felix.audit.store import flush_pending

    n = await flush_pending(_settings)
    logger.info("audit_flush count=%s", n)


@broker.task(schedule=[{"cron": "*/1 * * * *"}])
@_instrumented("flush_usage")
async def flush_usage() -> None:
    """Drain buffered usage (token) events to Postgres."""
    from felix.usage.store import flush_pending

    n = await flush_pending(_settings)
    logger.info("usage_flush count=%s", n)


@broker.task(schedule=[{"cron": "* * * * *"}])
@_instrumented("run_scheduled_jobs")
async def run_scheduled_jobs() -> None:
    """Fire due cron jobs (enabled rows only)."""
    from felix.jobs.scheduler import run_due_jobs_all_tenants

    fired = await run_due_jobs_all_tenants(_settings)
    if fired:
        logger.info("jobs_fired count=%s", fired)


@broker.task(schedule=[{"cron": "*/15 * * * *"}])
@_instrumented("consolidate_memory")
async def consolidate_memory() -> None:
    """Exact content-hash dedupe of active memory facts (not LLM merge)."""
    from felix.memory.consolidation import consolidate_pools

    n = await consolidate_pools(_settings)
    logger.info("memory_consolidate superseded=%s", n)


@broker.task(schedule=[{"cron": "0 3 * * *"}])
@_instrumented("retention_sweep")
async def retention_sweep() -> None:
    """Prune audit_events / expired plans / optional memory_vectors."""
    from felix.jobs.retention import run_retention_sweep

    await run_retention_sweep(_settings)


@broker.task(schedule=[{"cron": "*/30 * * * *"}])
@_instrumented("anomaly_scan")
async def anomaly_scan() -> None:
    """Detect tenant-level usage anomalies."""
    from felix.jobs.anomaly import run_anomaly_scan_all_tenants

    await run_anomaly_scan_all_tenants(_settings)


@broker.task(schedule=[{"cron": "*/10 * * * *"}])
@_instrumented("continuous_eval")
async def continuous_eval() -> None:
    """Online-benchmark active canaries against sampled traffic."""
    from felix.jobs.continuous_eval import run_continuous_eval_all_tenants

    await run_continuous_eval_all_tenants(_settings)


@broker.task(schedule=[{"cron": "* * * * *"}])
@_instrumented("fiber_scheduler")
async def fiber_scheduler() -> None:
    """Resume due durable fibers (step / stash / sleep)."""
    from felix.durability.fibers import resume_due_fibers

    await resume_due_fibers(_settings)
