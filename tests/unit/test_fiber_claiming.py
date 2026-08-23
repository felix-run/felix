"""Durable fibers must not run the same step twice.

`resume_due_fibers` selected every fiber in ('running','pending') with no lock, no
limit, and no claim — while a fiber stayed `running` for the duration of its step. The
scheduler fires every minute, so a step still running at the next tick was picked up and
invoked again, concurrently, on a single node; with two workers it was guaranteed. The
`invoke` op runs a full agent with tools, so that means duplicated emails, duplicated
charges, duplicated model spend.
"""

from __future__ import annotations

import asyncio

import pytest
from felix.config import Settings
from felix.durability import fibers
from felix.durability.fibers import (
    FIBER_BATCH,
    FIBER_LEASE_MS,
    create_fiber,
    now_ms,
    resume_due_fibers,
)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database_url="memory://fibers",
        object_store="memory",
        allow_insecure=True,
        auth_mode="none",
        environment="development",
    )


@pytest.fixture(autouse=True)
def _clean() -> None:
    fibers._memory_fibers.clear()


async def _slow_fiber(settings: Settings) -> str:
    row = await create_fiber(
        settings,
        "default",
        status="running",
        state={"steps": [{"op": "complete"}], "cursor": 0},
    )
    return str(row["id"])


# --- the claim ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_claimed_fiber_is_not_picked_up_again(settings: Settings) -> None:
    """The regression: an in-flight step must be invisible to the next tick."""
    await create_fiber(
        settings,
        "default",
        status="running",
        state={"steps": [{"op": "complete"}], "cursor": 0},
    )

    started = asyncio.Event()
    release = asyncio.Event()
    seen: list[str] = []
    original = fibers._run_fiber_step

    async def _slow_step(s: Settings, row: dict) -> dict:
        seen.append(str(row["id"]))
        started.set()
        await release.wait()
        return await original(s, row)

    fibers._run_fiber_step = _slow_step  # type: ignore[assignment]
    try:
        first = asyncio.create_task(resume_due_fibers(settings))
        await asyncio.wait_for(started.wait(), timeout=2)
        # a concurrent sweep, exactly what the every-minute scheduler does
        second = await resume_due_fibers(settings)
        assert second == 0, "a fiber already being stepped must not be claimed again"
        release.set()
        await asyncio.wait_for(first, timeout=2)
    finally:
        fibers._run_fiber_step = original  # type: ignore[assignment]

    assert seen.count(seen[0]) == 1, "the step ran more than once"


@pytest.mark.asyncio
async def test_claim_is_released_after_the_step(settings: Settings) -> None:
    """A still-runnable fiber must be claimable again on the next tick."""
    await create_fiber(
        settings,
        "default",
        status="running",
        state={"steps": [{"op": "sleep", "delay_ms": 0}, {"op": "complete"}], "cursor": 0},
    )
    assert await resume_due_fibers(settings) >= 1
    assert await resume_due_fibers(settings) >= 1


@pytest.mark.asyncio
async def test_expired_claim_is_reclaimed(settings: Settings) -> None:
    """A worker killed mid-step must not strand the fiber forever."""
    row = await create_fiber(
        settings,
        "default",
        status="running",
        state={"steps": [{"op": "complete"}], "cursor": 0},
    )
    stored = fibers._memory_fibers[("default", row["id"])]
    stored["lease_owner"] = "dead-worker"
    stored["lease_until"] = now_ms() - 1  # already expired

    assert await resume_due_fibers(settings) >= 1


@pytest.mark.asyncio
async def test_live_claim_from_another_worker_is_respected(settings: Settings) -> None:
    row = await create_fiber(
        settings,
        "default",
        status="running",
        state={"steps": [{"op": "complete"}], "cursor": 0},
    )
    stored = fibers._memory_fibers[("default", row["id"])]
    stored["lease_owner"] = "other-worker"
    stored["lease_until"] = now_ms() + FIBER_LEASE_MS

    assert await resume_due_fibers(settings) == 0


@pytest.mark.asyncio
async def test_sweep_is_bounded(settings: Settings) -> None:
    """An unbounded SELECT loaded the whole backlog into memory every minute."""
    for _ in range(FIBER_BATCH + 10):
        await create_fiber(
            settings,
            "default",
            status="pending",
            state={"steps": [{"op": "complete"}], "cursor": 0},
        )
    assert await resume_due_fibers(settings) == FIBER_BATCH


# --- version CAS ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_write_is_discarded(settings: Settings) -> None:
    """A lost update can rewind `cursor` and replay a step that already ran."""
    row = await create_fiber(
        settings,
        "default",
        status="running",
        state={"steps": [{"op": "complete"}], "cursor": 0},
    )
    key = ("default", row["id"])
    fresh = dict(fibers._memory_fibers[key])

    # writer A advances the fiber
    a = dict(fresh)
    a["state_json"] = {"steps": [{"op": "complete"}], "cursor": 5}
    await fibers._save_fiber(settings, a)
    assert fibers._memory_fibers[key]["state_json"]["cursor"] == 5

    # writer B holds the pre-A snapshot and tries to write cursor back to 1
    b = dict(fresh)
    b["state_json"] = {"steps": [{"op": "complete"}], "cursor": 1}
    await fibers._save_fiber(settings, b)
    assert fibers._memory_fibers[key]["state_json"]["cursor"] == 5, "stale write rewound the cursor"


@pytest.mark.asyncio
async def test_failed_step_releases_the_claim(settings: Settings) -> None:
    await create_fiber(
        settings,
        "default",
        status="running",
        state={"steps": [{"op": "complete"}], "cursor": 0},
    )
    original = fibers._run_fiber_step

    async def _boom(s: Settings, row: dict) -> dict:
        raise RuntimeError("step exploded")

    fibers._run_fiber_step = _boom  # type: ignore[assignment]
    try:
        await resume_due_fibers(settings)
    finally:
        fibers._run_fiber_step = original  # type: ignore[assignment]

    stored = next(iter(fibers._memory_fibers.values()))
    assert stored["lease_until"] is None, "a failed step must not strand the claim"
