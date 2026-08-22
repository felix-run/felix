"""Felix worker tasks — audit, cron, memory, retention, eval, fibers."""

from __future__ import annotations

import logging

from felix.config import get_settings
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
    from felix.plugins import get_registry, load_optional_plugins
    from felix.secrets import hydrate_secrets

    await hydrate_secrets(_settings)
    load_optional_plugins()
    _register_plugin_cron_tasks()
    logger.info(
        "worker_startup secrets_backend=%s plugins=%s",
        _settings.secrets_backend,
        len(get_registry().plugins),
    )


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
async def flush_audit() -> None:
    """Drain buffered audit events to Postgres."""
    from felix.audit.store import flush_pending

    n = await flush_pending(_settings)
    logger.info("audit_flush count=%s", n)


@broker.task(schedule=[{"cron": "*/1 * * * *"}])
async def flush_usage() -> None:
    """Drain buffered usage (token) events to Postgres."""
    from felix.usage.store import flush_pending

    n = await flush_pending(_settings)
    logger.info("usage_flush count=%s", n)


@broker.task(schedule=[{"cron": "* * * * *"}])
async def run_scheduled_jobs() -> None:
    """Fire due cron jobs (enabled rows only)."""
    from felix.jobs.scheduler import run_due_jobs

    await run_due_jobs(_settings)


@broker.task(schedule=[{"cron": "*/15 * * * *"}])
async def consolidate_memory() -> None:
    """Exact content-hash dedupe of active memory facts (not LLM merge)."""
    from felix.memory.consolidation import consolidate_pools

    n = await consolidate_pools(_settings)
    logger.info("memory_consolidate superseded=%s", n)


@broker.task(schedule=[{"cron": "0 3 * * *"}])
async def retention_sweep() -> None:
    """Prune audit_events / expired plans / optional memory_vectors."""
    from felix.jobs.retention import run_retention_sweep

    await run_retention_sweep(_settings)


@broker.task(schedule=[{"cron": "*/30 * * * *"}])
async def anomaly_scan() -> None:
    """Detect tenant-level usage anomalies."""
    from felix.jobs.anomaly import run_anomaly_scan

    await run_anomaly_scan(_settings)


@broker.task(schedule=[{"cron": "*/10 * * * *"}])
async def continuous_eval() -> None:
    """Online-benchmark active canaries against sampled traffic."""
    from felix.jobs.continuous_eval import run_continuous_eval

    await run_continuous_eval(_settings)


@broker.task(schedule=[{"cron": "* * * * *"}])
async def fiber_scheduler() -> None:
    """Resume due durable fibers (step / stash / sleep)."""
    from felix.durability.fibers import resume_due_fibers

    await resume_due_fibers(_settings)
