"""Continuous eval spends only when there is something to score."""

from __future__ import annotations

from typing import Any

import pytest
from felix.audit import store as audit_store
from felix.config import Settings
from felix.jobs import continuous_eval

from tests.support.factories import make_settings

CANARY = {"name": "quick", "canary_version": 2, "canary_weight": 10}


async def _arrange(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, *, event_type: str, payload: dict[str, Any]
) -> list[dict[str, Any]]:
    """One active canary, one audit row written the way a turn writes it, and a spy over `start_run`.

    The audit row goes through the real store, so the sample is read from the shape the store
    returns. A hand-built row keyed `payload` passed here while production read `payload_json`.
    """
    started: list[dict[str, Any]] = []

    async def _active(_settings: object, _tenant: str) -> list[dict[str, Any]]:
        return [CANARY]

    async def _start_run(_settings: object, **kwargs: Any) -> dict[str, Any]:
        started.append(kwargs)
        return {"id": "r1", "pass_count": 1, "fail_count": 0, "error_count": 0}

    monkeypatch.setattr(continuous_eval.manifest_store, "list_active", _active)
    monkeypatch.setattr(continuous_eval, "start_run", _start_run)
    audit_store.record_event(settings, "default", event_type, payload=payload)
    await audit_store.flush_pending(settings)
    return started


async def test_an_idle_tenant_starts_no_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no user turn to replay it sent a literal "ping" to every canary, every ten minutes."""
    settings = make_settings()
    started = await _arrange(monkeypatch, settings, event_type="tool_call", payload={"tool": "calculator"})

    result = await continuous_eval.run_continuous_eval(settings, tenant_id="default")

    assert started == []
    assert result == {"runs": 0, "samples": 0, "reason": "no_samples"}


async def test_a_recent_user_turn_is_scored_against_the_canary(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = make_settings()
    started = await _arrange(
        monkeypatch, settings, event_type="user_input", payload={"user_input": "what is 2+2"}
    )

    result = await continuous_eval.run_continuous_eval(settings, tenant_id="default")

    assert [(s["candidate_manifest"], s["manifest_version"]) for s in started] == [("quick", 2)]
    assert (result["runs"], result["samples"]) == (1, 1)
