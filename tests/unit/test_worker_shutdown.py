"""What a stopping worker owes: its buffered rows written, and the time to write them."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml
from felix.audit import store as audit_store
from felix.usage import store as usage_store
from felix_worker.main import worker_args

from tests.support.compose_yaml import load_compose
from tests.support.factories import make_settings

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"


async def test_shutdown_writes_what_the_worker_still_holds(monkeypatch: pytest.MonkeyPatch) -> None:
    """The minute cron was the worker's only flush, so a restart dropped what it had buffered
    since: the usage and audit rows of durable runs and jobs, the spend no one is watching as it
    happens. The rows here are recorded by the drain itself, because that is the order that
    matters: a flush ahead of the drain would miss what the last runs wrote."""
    import felix_worker.tasks as tasks

    settings = make_settings()
    monkeypatch.setattr(tasks, "_settings", settings)

    async def _last_run_finishes() -> None:
        usage_store.record_tokens(
            settings, tenant_id="default", manifest_id="quick", model_id="m", tokens_input=7, tokens_output=3
        )
        audit_store.record_event(settings, "default", "tool_call", payload={"tool": "calculator"})

    monkeypatch.setattr(tasks, "_stop_fiber_loop", _last_run_finishes)

    await tasks._on_worker_shutdown(None)

    assert (usage_store.pending_count(), len(audit_store.pending_buffer())) == (0, 0)
    usage, _ = await usage_store.query(settings, "default")
    assert [(r["tokens_input"], r["tokens_output"]) for r in usage] == [(7, 3)]
    audit, _ = await audit_store.query(settings, "default")
    assert [e["event_type"] for e in audit] == ["tool_call"]


async def test_a_fiber_loop_that_died_does_not_stop_the_flush(monkeypatch: pytest.MonkeyPatch) -> None:
    import felix_worker.tasks as tasks

    settings = make_settings()
    monkeypatch.setattr(tasks, "_settings", settings)

    async def _died() -> None:
        raise RuntimeError("the loop's own failure")

    monkeypatch.setattr(tasks, "_fiber_loop", asyncio.ensure_future(_died()))
    monkeypatch.setattr(tasks, "_fiber_loop_stop", asyncio.Event())
    usage_store.record_tokens(
        settings, tenant_id="default", manifest_id="quick", model_id="m", tokens_input=1
    )

    await tasks._on_worker_shutdown(None)

    assert usage_store.pending_count() == 0


def _compose_worker_grace() -> int:
    """Compose kills a container 10s after `stop` unless told otherwise."""
    grace = str(
        load_compose(DEPLOY / "docker" / "compose.yml")["services"]["worker"].get("stop_grace_period", "10s")
    )
    seconds = re.fullmatch(r"(\d+)s", grace)
    assert seconds, f"stop_grace_period {grace!r} is not whole seconds"
    return int(seconds.group(1))


def _helm_worker_grace() -> int:
    """Kubernetes' default is 30s."""
    values = yaml.safe_load((DEPLOY / "helm" / "felix" / "values.yaml").read_text(encoding="utf-8"))
    return int(values["worker"].get("terminationGracePeriodSeconds", 30))


@pytest.mark.parametrize("grace", [_compose_worker_grace, _helm_worker_grace], ids=["compose", "helm"])
def test_the_worker_is_given_longer_than_its_drain_and_flush(grace: Callable[[], int]) -> None:
    """The worker gives running durable runs `FIBER_LOOP_DRAIN_S` to finish and then flushes;
    killed before that, the runs are lost and so are their rows. Compose had no grace period at all.

    Measured against what the worker asks Taskiq for (`felix_worker.shutdown`), which is the
    drain, the stop, the flush and a margin: a container killed inside the time its own worker
    was told it had is the same loss by another route.
    """
    args = worker_args()
    assert grace() > args.wait_tasks_timeout + args.shutdown_timeout
