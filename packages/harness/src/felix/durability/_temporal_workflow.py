"""Temporal workflow and activity definitions (``felix-harness[temporal]`` only).

Separate from `durability/temporal.py` for one reason: `@workflow.run` refuses a class
declared inside a function — *"Local classes unsupported ... we need to have the class
globally referenceable by name"* — because the worker re-imports it by name inside the
sandbox. So the class has to live at module scope, which means `temporalio` has to be
imported at module scope, which the repo otherwise forbids.

This module is the exception, and it is an enforced one: nothing may import it at module
scope (`tests/unit/test_invariants.py`), and `scripts/lean-import-check.py` skips it by
name. Both would fail if it were ever reached from a lean install.

The definitions used to be built inside a function, which raised `ValueError` on every
call. `start_fiber_workflow` is wrapped in `except Exception` by its caller, so
`FELIX_DURABILITY=temporal` silently ran on the Postgres fiber scheduler instead, and
`felix temporal-worker` failed outright. The one test covering this monkeypatched
`start_fiber_workflow` — the function that could not run.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import activity, workflow

# Mirrors `fibers.FIBER_TERMINAL_STATUSES`; a literal because this module loads inside the
# workflow sandbox. `tests/unit/test_invariants.py` keeps the two equal.
_TERMINAL = frozenset({"completed", "failed", "expired", "dead"})


@activity.defn(name="felix_fiber_step")
async def fiber_step(row: dict[str, Any]) -> dict[str, Any]:
    """Advance one fiber step.

    Imports inside the function deliberately: an activity runs outside the workflow
    sandbox, but keeping the import here means loading this module costs nothing beyond
    `temporalio` itself.
    """
    from felix.config import get_settings
    from felix.durability.fibers import advance_fiber

    return await advance_fiber(get_settings(), row)


@workflow.defn(name="felix_durable_fiber")
class DurableFiberWorkflow:
    """Drive `advance_fiber` until the fiber reaches a terminal status.

    The step logic lives in `durability/fibers.py` and is shared with the Postgres
    scheduler, so the two backends cannot disagree about what a step means — only about
    what drives them.
    """

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
            if status in _TERMINAL:
                return current
            if status == "sleeping":
                wake = int(current.get("wake_at") or 0)
                now_ms = int(workflow.now().timestamp() * 1000)
                delay_ms = max(1, wake - now_ms) if wake else 1
                await workflow.sleep(timedelta(milliseconds=delay_ms))
                current["status"] = "running"
                current["wake_at"] = None


__all__ = ["DurableFiberWorkflow", "fiber_step"]
