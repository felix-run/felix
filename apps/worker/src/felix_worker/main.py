"""Taskiq worker + scheduler entrypoints for Felix background jobs."""

from __future__ import annotations

import sys
from typing import Any


def worker_args() -> Any:
    """What the worker hands Taskiq. A function so a test can read what production passes."""
    from taskiq.cli.common_args import LogLevel
    from taskiq.cli.worker.args import WorkerArgs

    from felix_worker.shutdown import SHUTDOWN_TIMEOUT_S, WAIT_TASKS_TIMEOUT_S

    return WorkerArgs(
        broker="felix_worker.tasks:broker",
        modules=["felix_worker.tasks"],
        workers=1,
        log_level=LogLevel.INFO,
        # Both of Taskiq's defaults cut the shutdown handler off: see `felix_worker.shutdown`.
        shutdown_timeout=SHUTDOWN_TIMEOUT_S,
        wait_tasks_timeout=WAIT_TASKS_TIMEOUT_S,
    )


def main() -> None:
    """Start the Taskiq worker process (consumes queued tasks)."""
    from taskiq.cli.worker.run import run_worker

    sys.exit(run_worker(worker_args()) or 0)


def scheduler_main() -> None:
    """Start the Taskiq scheduler (enqueues labeled cron tasks)."""
    import asyncio

    from felix.config import get_settings

    get_settings().stamp_process_role("scheduler")

    from taskiq.cli.common_args import LogLevel
    from taskiq.cli.scheduler.args import SchedulerArgs
    from taskiq.cli.scheduler.run import run_scheduler

    args = SchedulerArgs(
        scheduler="felix_worker.tasks:scheduler",
        modules=["felix_worker.tasks"],
        log_level=LogLevel.INFO,
        skip_first_run=False,
    )
    # taskiq 0.12+ exposes async run_scheduler (CLI uses asyncio.run).
    asyncio.run(run_scheduler(args))


if __name__ == "__main__":
    main()
