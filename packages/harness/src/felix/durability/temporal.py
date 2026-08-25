"""Optional Temporal backend for durable fibers (``felix-harness[temporal]``)."""

from __future__ import annotations

from typing import Any

from felix.config import Settings

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


def _defs() -> tuple[Any, Any]:
    """The workflow class and activity, imported rather than built.

    They used to be declared inside this function. `@workflow.run` rejects a class
    declared in a function body -- the worker re-imports it by name inside the sandbox
    -- so every call raised `ValueError`, and both entry points below were dead:
    `start_fiber_workflow` failed into its caller's `except Exception`, which logged a
    warning and let the Postgres fiber scheduler run the chat, and `felix
    temporal-worker` failed outright.

    No cache here any more: a module import is already cached by `sys.modules`, and the
    hand-rolled one only existed to avoid rebuilding classes that should never have
    been built per call.
    """
    from felix.durability._temporal_workflow import DurableFiberWorkflow, fiber_step

    return (DurableFiberWorkflow, fiber_step)


__all__ = ["TASK_QUEUE", "run_worker", "start_fiber_workflow"]
