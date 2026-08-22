"""Taskiq worker + scheduler entrypoints for Felix background jobs."""

from __future__ import annotations

import sys


def main() -> None:
    """Start the Taskiq worker process (consumes queued tasks)."""
    from taskiq.cli.common_args import LogLevel
    from taskiq.cli.worker.args import WorkerArgs
    from taskiq.cli.worker.run import run_worker

    args = WorkerArgs(
        broker="felix_worker.tasks:broker",
        modules=["felix_worker.tasks"],
        workers=1,
        log_level=LogLevel.INFO,
    )
    sys.exit(run_worker(args) or 0)


def scheduler_main() -> None:
    """Start the Taskiq scheduler (enqueues labeled cron tasks)."""
    from taskiq.cli.common_args import LogLevel
    from taskiq.cli.scheduler.args import SchedulerArgs
    from taskiq.cli.scheduler.run import run_scheduler

    args = SchedulerArgs(
        scheduler="felix_worker.tasks:scheduler",
        modules=["felix_worker.tasks"],
        log_level=LogLevel.INFO,
        skip_first_run=False,
    )
    sys.exit(run_scheduler(args) or 0)


if __name__ == "__main__":
    main()
