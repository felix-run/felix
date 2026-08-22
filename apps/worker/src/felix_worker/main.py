"""Taskiq worker entry for Felix background jobs."""

from __future__ import annotations

import sys


def main() -> None:
    """Start the Taskiq worker process."""
    # Delegate to the Taskiq CLI so schedule sources and ACK semantics match docs.
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


if __name__ == "__main__":
    main()
