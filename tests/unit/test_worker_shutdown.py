"""What a stopping worker owes: its buffered rows written, and the time to write them."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from felix.durability.fibers import FIBER_LOOP_DRAIN_S
from felix.usage import store as usage_store

from tests.support.compose_yaml import load_compose
from tests.support.factories import make_settings

COMPOSE = Path(__file__).resolve().parents[2] / "deploy" / "docker" / "compose.yml"


async def test_shutdown_writes_the_usage_the_worker_still_holds(monkeypatch: pytest.MonkeyPatch) -> None:
    """The minute cron was the worker's only flush, so a restart dropped what it had buffered
    since: the usage of durable runs and jobs, the spend no one is watching as it happens."""
    import felix_worker.tasks as tasks

    settings = make_settings()
    monkeypatch.setattr(tasks, "_settings", settings)
    usage_store.clear_memory()
    usage_store.record_tokens(
        settings, tenant_id="default", manifest_id="quick", model_id="m", tokens_input=7, tokens_output=3
    )
    assert usage_store.pending_count() == 1

    await tasks._on_worker_shutdown(None)

    assert usage_store.pending_count() == 0
    rows, _ = await usage_store.query(settings, "default")
    assert [(r["tokens_input"], r["tokens_output"]) for r in rows] == [(7, 3)]


def test_compose_gives_the_worker_longer_than_its_drain() -> None:
    """Compose kills a container 10s after `stop` unless told otherwise, and the worker gives
    running durable runs `FIBER_LOOP_DRAIN_S` to finish before it flushes."""
    grace = load_compose(COMPOSE)["services"]["worker"].get("stop_grace_period", "10s")
    seconds = re.fullmatch(r"(\d+)s", str(grace))
    assert seconds, f"stop_grace_period {grace!r} is not whole seconds"
    assert int(seconds.group(1)) > FIBER_LOOP_DRAIN_S
