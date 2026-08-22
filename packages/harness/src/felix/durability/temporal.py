"""Optional Temporal backend for durable fibers (``felix-harness[temporal]``)."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from felix.config import Settings

logger = logging.getLogger("felix.durability.temporal")

TASK_QUEUE = "felix-fibers"


async def start_fiber_workflow(settings: Settings, fiber: dict[str, Any]) -> str:
    """Start a Temporal workflow that drives ``advance_fiber`` until completion."""
    try:
        from temporalio.client import Client
    except ImportError as exc:
        raise RuntimeError(
            "Temporal durability requires felix-harness[temporal] (uv sync --extra temporal)"
        ) from exc

    workflow_cls, _activity = _defs()
    client = await Client.connect(
        settings.temporal_host,
        namespace=settings.temporal_namespace or "default",
    )
    handle = await client.start_workflow(
        workflow_cls.run,
        fiber,
        id=f"felix-fiber-{fiber['tenant_id']}-{fiber['id']}",
        task_queue=TASK_QUEUE,
    )
    return str(handle.id)


async def run_worker(settings: Settings) -> None:
    """Run a Temporal worker on ``felix-fibers`` (blocking)."""
    try:
        from temporalio.client import Client
        from temporalio.worker import Worker
    except ImportError as exc:
        raise RuntimeError(
            "Temporal durability requires felix-harness[temporal] (uv sync --extra temporal)"
        ) from exc

    workflow_cls, activity = _defs()
    client = await Client.connect(
        settings.temporal_host,
        namespace=settings.temporal_namespace or "default",
    )
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[workflow_cls],
        activities=[activity],
    )
    await worker.run()


_cached_defs: tuple[Any, Any] | None = None


def _defs() -> tuple[Any, Any]:
    global _cached_defs
    if _cached_defs is not None:
        return _cached_defs
    from temporalio import activity, workflow

    @activity.defn(name="felix_fiber_step")
    async def fiber_step(row: dict[str, Any]) -> dict[str, Any]:
        from felix.config import get_settings
        from felix.durability.fibers import advance_fiber

        return await advance_fiber(get_settings(), row)

    @workflow.defn(name="felix_durable_fiber")
    class DurableFiberWorkflow:
        @workflow.run
        async def run(self, row: dict[str, Any]) -> dict[str, Any]:
            current = dict(row)
            while True:
                current = await workflow.execute_activity(
                    "felix_fiber_step",
                    current,
                    start_to_close_timeout=timedelta(minutes=15),
                )
                status = str(current.get("status") or "")
                if status in {"completed", "failed", "expired"}:
                    return current
                if status == "sleeping":
                    wake = int(current.get("wake_at") or 0)
                    now_ms = int(workflow.now().timestamp() * 1000)
                    delay_ms = max(1, wake - now_ms) if wake else 1
                    await workflow.sleep(timedelta(milliseconds=delay_ms))
                    current["status"] = "running"
                    current["wake_at"] = None

    _cached_defs = (DurableFiberWorkflow, fiber_step)
    return _cached_defs


__all__ = ["TASK_QUEUE", "run_worker", "start_fiber_workflow"]
