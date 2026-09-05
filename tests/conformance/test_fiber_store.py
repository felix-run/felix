"""Fiber store contract: the retry count survives the round trip on both backends.

The unit tests prove the scheduler's arithmetic on the memory twin; this proves the
Postgres row carries `attempts` through claim, failure, backoff and burial the same way.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.durability import fibers

BACKENDS = ["memory", "postgres"]
parametrized = pytest.mark.parametrize("fiber_settings", BACKENDS, indirect=True)
TENANT = "conformance"


@parametrized
@pytest.mark.asyncio
async def test_attempts_persist_through_backoff_to_dead(
    fiber_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = fiber_settings.model_copy(update={"fiber_max_attempts": 2})
    clock = {"ms": 1_800_000_000_000}
    monkeypatch.setattr(fibers, "now_ms", lambda: clock["ms"])

    async def boom(settings: Any, row: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("store down")

    monkeypatch.setattr(fibers, "_run_fiber_step", boom)
    created = await fibers.create_fiber(settings, TENANT, state={"steps": [{"op": "complete"}], "cursor": 0})
    fiber_id = str(created["id"])
    stored = await fibers.get_fiber(settings, TENANT, fiber_id)
    assert stored is not None and stored["attempts"] == 0, "the column's default did not round-trip"

    assert await fibers.resume_due_fibers(settings) == 1
    row = await fibers.get_fiber(settings, TENANT, fiber_id)
    assert row is not None
    assert (row["status"], row["attempts"], row["lease_until"]) == ("sleeping", 1, None)
    assert row["wake_at"] == clock["ms"] + fibers.retry_delay_ms(1)

    clock["ms"] = int(row["wake_at"])
    assert await fibers.resume_due_fibers(settings) == 1
    row = await fibers.get_fiber(settings, TENANT, fiber_id)
    assert row is not None
    assert (row["status"], row["attempts"], row["wake_at"]) == ("dead", 2, None)

    clock["ms"] += fibers.FIBER_RETRY_MAX_MS
    assert await fibers.resume_due_fibers(settings) == 0, "dead fibers are never claimed"


@parametrized
@pytest.mark.asyncio
async def test_attempts_persist_when_the_save_itself_fails(
    fiber_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bookkeeping write that avoids `state_json` lands on both backends."""
    settings = fiber_settings.model_copy(update={"fiber_max_attempts": 2})
    clock = {"ms": 1_800_000_000_000}
    monkeypatch.setattr(fibers, "now_ms", lambda: clock["ms"])

    async def unsaveable(settings: Any, row: dict[str, Any]) -> None:
        raise RuntimeError("state_json is not JSON serialisable")

    created = await fibers.create_fiber(
        settings, TENANT, state={"steps": [{"op": "stash", "data": {}}, {"op": "complete"}], "cursor": 0}
    )
    monkeypatch.setattr(fibers, "_save_fiber", unsaveable)
    fiber_id = str(created["id"])

    assert await fibers.resume_due_fibers(settings) == 1
    row = await fibers.get_fiber(settings, TENANT, fiber_id)
    assert row is not None
    assert (row["status"], row["attempts"], row["lease_until"]) == ("sleeping", 1, None)
    clock["ms"] = int(row["wake_at"])
    assert await fibers.resume_due_fibers(settings) == 1
    row = await fibers.get_fiber(settings, TENANT, fiber_id)
    assert row is not None
    assert (row["status"], row["attempts"]) == ("dead", 2)
