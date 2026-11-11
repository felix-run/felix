"""Continuous eval spends only when there is something to score."""

from __future__ import annotations

from typing import Any

import pytest
from felix.jobs import continuous_eval

from tests.support.factories import make_settings

CANARY = {"name": "quick", "canary_version": 2, "canary_weight": 10}


def _arrange(monkeypatch: pytest.MonkeyPatch, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One active canary, `events` as the tenant's recent audit rows, and a spy over `start_run`."""
    started: list[dict[str, Any]] = []

    async def _active(_settings: object, _tenant: str) -> list[dict[str, Any]]:
        return [CANARY]

    async def _events(_settings: object, _tenant: str, **_kw: object) -> tuple[list[dict[str, Any]], None]:
        return events, None

    async def _start_run(_settings: object, **kwargs: Any) -> dict[str, Any]:
        started.append(kwargs)
        return {"id": "r1", "pass_count": 1, "fail_count": 0, "error_count": 0}

    monkeypatch.setattr(continuous_eval.manifest_store, "list_active", _active)
    monkeypatch.setattr(continuous_eval.audit_store, "list_events", _events)
    monkeypatch.setattr(continuous_eval, "start_run", _start_run)
    return started


async def test_an_idle_tenant_starts_no_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no user turn to replay it sent a literal "ping" to every canary, every ten minutes."""
    started = _arrange(monkeypatch, events=[{"payload": {"tool": "calculator"}}])

    result = await continuous_eval.run_continuous_eval(make_settings(), tenant_id="default")

    assert started == []
    assert result == {"runs": 0, "samples": 0, "reason": "no_samples"}


async def test_a_recent_user_turn_is_scored_against_the_canary(monkeypatch: pytest.MonkeyPatch) -> None:
    started = _arrange(monkeypatch, events=[{"payload": {"user_input": "what is 2+2"}}])

    result = await continuous_eval.run_continuous_eval(make_settings(), tenant_id="default")

    assert [(s["candidate_manifest"], s["manifest_version"]) for s in started] == [("quick", 2)]
    assert (result["runs"], result["samples"]) == (1, 1)
