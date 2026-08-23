"""Periodic flush of the process-local audit and usage buffers.

`emit_agent_audit` and `record_usage` are called from the agent loop, which runs in the
**API** process — but the only `flush_pending` callers used to be Taskiq cron tasks in
the **worker**. In any deployment where those are separate containers (Compose, Helm),
the worker's buffers were always empty and the API's were never drained: the audit trail
was silently empty and the buffer grew for the life of the process.

Every process that emits events must therefore also flush them.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger("felix.flush")


async def flush_all(settings: Any) -> tuple[int, int]:
    """Flush audit and usage once. Returns ``(audit_rows, usage_rows)``.

    Each is attempted independently so a failure in one does not strand the other; both
    stores re-queue their batch on failure, so nothing is lost by swallowing here.
    """
    from felix.audit import store as audit_store
    from felix.usage import store as usage_store

    audit_n = usage_n = 0
    try:
        audit_n = await audit_store.flush_pending(settings)
    except Exception:
        logger.warning("audit flush failed; batch re-queued", exc_info=True)
    try:
        usage_n = await usage_store.flush_pending(settings)
    except Exception:
        logger.warning("usage flush failed; batch re-queued", exc_info=True)
    return audit_n, usage_n


async def run_flush_loop(settings: Any, *, interval_s: float) -> None:
    """Flush on an interval until cancelled. Intended for the API lifespan."""
    while True:
        try:
            await asyncio.sleep(interval_s)
            await flush_all(settings)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Never let the loop die — it is the only writer in the API process.
            logger.warning("flush loop iteration failed", exc_info=True)


def _report_flush_task_exit(task: asyncio.Task[None]) -> None:
    """Surface a crashed flush loop.

    Retrieving the exception also stops asyncio's "Task exception was never retrieved"
    warning at interpreter shutdown, but the point is the log line: if this loop dies,
    the process stops writing audit and usage events and nothing else would say so.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("flush loop exited unexpectedly; events are no longer being written", exc_info=exc)


def start_flush_task(settings: Any) -> asyncio.Task[None] | None:
    """Start the background flush loop, or ``None`` when disabled.

    The caller must hold the returned task (the API keeps it on ``app.state``); asyncio
    only holds a weak reference, so a dropped task can be garbage collected mid-flight.
    """
    interval = float(getattr(settings, "audit_flush_seconds", 0) or 0)
    if interval <= 0:
        return None
    task = asyncio.create_task(run_flush_loop(settings, interval_s=interval))
    task.add_done_callback(_report_flush_task_exit)
    return task


async def stop_flush_task(task: asyncio.Task[None] | None, settings: Any) -> None:
    """Cancel the loop and drain what is left, so shutdown does not lose events."""
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            logger.debug("flush loop cancelled during shutdown")
        except Exception:
            logger.warning("flush loop raised during shutdown", exc_info=True)
    await flush_all(settings)


__all__ = ["flush_all", "run_flush_loop", "start_flush_task", "stop_flush_task"]
