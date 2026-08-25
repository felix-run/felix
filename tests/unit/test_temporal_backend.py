"""The Temporal backend, tested where it was previously patched out.

`FELIX_DURABILITY=temporal` had never worked. `@workflow.run` rejects a class declared
inside a function — the worker re-imports it by name inside its sandbox — and the
definitions were built inside `_defs()`, so every call raised `ValueError`.

Both entry points were dead in different ways. `start_fiber_workflow` failed into its
caller's `except Exception`, which logged a warning and let the Postgres fiber scheduler
run the chat, so a deployment that asked for Temporal silently got fibers. `felix
temporal-worker` failed outright.

The one existing test passed throughout, because it monkeypatched `start_fiber_workflow`
— the function that could not run. That is the shape worth remembering: a test that
asserts the wiring around a component while replacing the component.

These do not need a Temporal server. What was broken was definition-time, and that is
exactly what a server-less test can reach.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.config import Settings

pytest.importorskip("temporalio", reason="felix-harness[temporal] not installed")


def _settings(**kw: Any) -> Settings:
    return Settings(database_url="memory://temporal", **kw)


def test_the_workflow_definitions_can_be_built() -> None:
    """The whole bug, in one assertion.

    `_defs()` raised `ValueError` on every call. Nothing noticed because both callers
    were either wrapped in `except Exception` or never exercised.
    """
    from felix.durability.temporal import _defs

    workflow_cls, activity_fn = _defs()
    assert workflow_cls.__name__ == "DurableFiberWorkflow"
    assert callable(activity_fn)


def test_the_workflow_class_is_globally_referenceable() -> None:
    """Temporal's actual requirement, asserted directly rather than via the error it
    produces: the worker re-imports the class by name, so a `<locals>` qualname means
    it cannot be found."""
    from felix.durability.temporal import _defs

    workflow_cls, _ = _defs()
    assert "<locals>" not in workflow_cls.run.__qualname__
    assert "<locals>" not in workflow_cls.__qualname__


def test_temporal_registers_the_names_the_workflow_calls() -> None:
    """The workflow executes the activity by string name. A rename on either side
    would leave the workflow waiting on an activity no worker serves."""
    from felix.durability.temporal import _defs

    workflow_cls, activity_fn = _defs()
    activity_defn = getattr(activity_fn, "__temporal_activity_definition", None)
    workflow_defn = getattr(workflow_cls, "__temporal_workflow_definition", None)
    assert activity_defn is not None, "the activity is not registered with Temporal"
    assert workflow_defn is not None, "the workflow is not registered with Temporal"
    assert activity_defn.name == "felix_fiber_step"
    assert workflow_defn.name == "felix_durable_fiber"


def test_the_activity_advances_a_fiber(monkeypatch: pytest.MonkeyPatch) -> None:
    """The activity is the seam between Temporal and the shared step logic — the same
    `advance_fiber` the Postgres scheduler drives, so the two backends cannot disagree
    about what a step means."""
    import asyncio

    from felix.durability import _temporal_workflow as defs

    seen: list[dict[str, Any]] = []

    async def _advance(_settings: Any, row: dict[str, Any]) -> dict[str, Any]:
        seen.append(row)
        return {**row, "status": "completed"}

    monkeypatch.setattr("felix.durability.fibers.advance_fiber", _advance)
    monkeypatch.setattr("felix.config.get_settings", _settings)

    out = asyncio.run(defs.fiber_step({"id": "f1", "status": "pending"}))
    assert seen and seen[0]["id"] == "f1"
    assert out["status"] == "completed"


# --- what the failure looked like from outside -----------------------------------


@pytest.mark.asyncio
async def test_a_failed_temporal_start_records_the_fallback() -> None:
    """A run that asked for Temporal and got fibers must say so in the row.

    `backend` was set inside the `try`, so a failed start left a row identical to one
    that never asked for Temporal at all. With the feature broken for as long as it
    was, nothing on the row distinguished "Temporal ran this" from "Temporal could not
    start and we quietly used the scheduler".
    """
    from felix.durability.fibers import get_fiber
    from felix.durability.runs import start_durable_chat
    from felix.manifests.schema import ExecutionSpec
    from felix.patterns.types import ChatMessage

    settings = _settings(durability="temporal")

    started = await start_durable_chat(
        settings,
        "t-fallback",
        manifest_id="quick",
        messages=[ChatMessage(role="user", content="hi")],
        thread_id=None,
        model_id=None,
        execution=ExecutionSpec(mode="durable"),
    )
    row = await get_fiber(settings, "t-fallback", started["resume_token"])
    assert row is not None
    state = row.get("state_json") or {}
    # No Temporal server is reachable from a test, so this is the fallback path.
    assert state.get("backend") == "fibers", state
    assert state.get("backend_fallback") == "temporal_start_failed", state


@pytest.mark.asyncio
async def test_the_fallback_still_leaves_a_runnable_fiber() -> None:
    """Degrading is only acceptable if the work still happens."""
    from felix.durability.fibers import get_fiber, resume_due_fibers
    from felix.durability.runs import start_durable_chat
    from felix.manifests.schema import ExecutionSpec
    from felix.patterns.types import ChatMessage

    settings = _settings(durability="temporal")
    started = await start_durable_chat(
        settings,
        "t-runnable",
        manifest_id="quick",
        messages=[ChatMessage(role="user", content="hi")],
        thread_id=None,
        model_id=None,
        execution=ExecutionSpec(mode="durable"),
    )
    await resume_due_fibers(settings)
    row = await get_fiber(settings, "t-runnable", started["resume_token"])
    assert row is not None
    assert row["status"] != "expired", "the fallback fiber was never picked up"
