"""A claim runs a fiber to its next suspension, not for exactly one op.

`resume_due_fibers` called `_step_with_lease` once per claimed row and `_run_fiber_step`
advanced exactly one op, so a fiber cost one `* * * * *` tick per step — and the last of
those ticks did no work at all, because `_run_fiber_step` only notices `cursor >= len(steps)`
and flips to `completed` on the sweep *after* the one that ran the final step.

A durable chat's `steps` has length one (`durability/runs.py:start_durable_chat`), so the
cheapest possible durable run took two ticks: around two minutes, the second of which was
pure scheduler latency. A *failure* terminates inside one sweep, so a failed run reached its
terminal state a full minute before a successful one.

Measured against the code before the change, by the same harness these tests use:

    durable chat (one invoke)   2 ticks -> 1
    stash then complete         2       -> 1
    three stashes               4       -> 1
    invoke then stash           3       -> 1
    two invokes                 3       -> 2   (one model turn per claim, deliberately)
    sleep then complete         2       -> 2   (a sleep *is* a suspension)
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.config import Settings
from felix.durability import fibers
from felix.durability.fibers import create_fiber, get_fiber, resume_due_fibers

TENANT = "default"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        auth_mode="none",
        allow_insecure=True,
        object_store="memory",
        database_url="memory://fiber-suspension",
    )


@pytest.fixture(autouse=True)
def _clean() -> None:
    fibers.reset_memory_fibers()


async def _ticks_to_finish(
    settings: Settings, steps: list[dict[str, Any]], limit: int = 10
) -> tuple[int, str]:
    """Sweeps until the fiber reaches a terminal status, and the status it reached."""
    created = await create_fiber(
        settings, TENANT, status="pending", state={"steps": steps, "cursor": 0, "stash": {}}
    )
    fiber_id = str(created["id"])
    for tick in range(1, limit + 1):
        await resume_due_fibers(settings)
        row = await get_fiber(settings, TENANT, fiber_id)
        assert row is not None
        if row["status"] in fibers.FIBER_TERMINAL_STATUSES:
            return tick, str(row["status"])
    raise AssertionError(f"{steps} never finished in {limit} sweeps")


@pytest.mark.asyncio
async def test_a_durable_chat_finishes_in_one_sweep(settings: Settings) -> None:
    """The shape that matters, spelled out on its own.

    `start_durable_chat` builds exactly this: a single `invoke`. It is the whole reason the
    entry was worth doing — every durable run in production is this shape, and half its
    latency was a tick spent noticing the run was over.
    """
    # `manifest_id` empty means the invoke records an empty answer without building an agent,
    # which is what keeps this a scheduler test rather than a model one.
    ticks, status = await _ticks_to_finish(settings, [{"op": "invoke", "manifest_id": ""}])
    assert (ticks, status) == (1, "completed")


@pytest.mark.asyncio
async def test_bookkeeping_steps_do_not_each_cost_a_tick(settings: Settings) -> None:
    """Three stashes took four sweeps: one per op, plus one to notice it was done."""
    ticks, status = await _ticks_to_finish(settings, [{"op": "stash", "data": {"i": i}} for i in range(3)])
    assert (ticks, status) == (1, "completed")


@pytest.mark.asyncio
async def test_a_second_invoke_waits_for_the_next_sweep(settings: Settings) -> None:
    """The bound that keeps this from being a fairness regression.

    `invoke` is the only op that can take seconds, so one claim runs at most one of them —
    wall-clock per fiber per sweep is exactly what it was. What the change removes is the
    ticks that were doing no work, not the scheduler's ability to interleave fibers.
    """
    two = [{"op": "invoke", "manifest_id": ""}, {"op": "invoke", "manifest_id": ""}]
    assert await _ticks_to_finish(settings, two) == (2, "completed")
    # …and the tail after an invoke is free, which is the same claim continuing.
    fibers.reset_memory_fibers()
    trailing = [{"op": "invoke", "manifest_id": ""}, {"op": "stash", "data": {"k": 1}}]
    assert await _ticks_to_finish(settings, trailing) == (1, "completed")


@pytest.mark.asyncio
async def test_a_sleep_still_suspends_the_claim(settings: Settings) -> None:
    """`sleep` is a suspension, so it ends the claim — the loop runs *to* suspension.

    Without this the loop would treat a sleep as something to spin through, and a fiber
    asking to wake in an hour would be woken immediately.
    """
    created = await create_fiber(
        settings,
        TENANT,
        status="pending",
        state={
            "steps": [{"op": "sleep", "delay_ms": 3_600_000}, {"op": "complete"}],
            "cursor": 0,
            "stash": {},
        },
    )
    fiber_id = str(created["id"])
    await resume_due_fibers(settings)

    row = await get_fiber(settings, TENANT, fiber_id)
    assert row is not None
    assert row["status"] == "sleeping", "the claim ran straight through a sleep"
    assert row["wake_at"] is not None and row["wake_at"] > fibers.now_ms(), row["wake_at"]
    assert await resume_due_fibers(settings) == 0, "a sleeping fiber was claimed before it was due"


@pytest.mark.asyncio
async def test_the_claim_is_released_once_the_sweep_is_done(settings: Settings) -> None:
    """Every save inside the loop holds the claim, so exactly one release has to close it.

    This is the half that would fail silently: a claim left behind makes the fiber
    unclaimable for `FIBER_LEASE_MS` — five minutes — which looks like the scheduler having
    stopped rather than like a bug.
    """
    created = await create_fiber(
        settings,
        TENANT,
        status="pending",
        state={"steps": [{"op": "sleep", "delay_ms": 3_600_000}], "cursor": 0, "stash": {}},
    )
    await resume_due_fibers(settings)

    row = await get_fiber(settings, TENANT, str(created["id"]))
    assert row is not None
    assert (row["lease_owner"], row["lease_until"]) == ("", None), "the claim outlived the sweep"


@pytest.mark.asyncio
async def test_a_steps_list_longer_than_the_budget_yields_rather_than_hogs(settings: Settings) -> None:
    """The backstop. A `steps` list long enough to hold the worker off its batch has to give
    the claim back and finish on a later sweep, not run to the end regardless."""
    steps: list[dict[str, Any]] = [
        {"op": "stash", "data": {"i": i}} for i in range(fibers.FIBER_MAX_OPS_PER_CLAIM + 4)
    ]
    created = await create_fiber(
        settings, TENANT, status="pending", state={"steps": steps, "cursor": 0, "stash": {}}
    )
    fiber_id = str(created["id"])

    await resume_due_fibers(settings)
    row = await get_fiber(settings, TENANT, fiber_id)
    assert row is not None
    assert row["status"] == "running", "the budget did not stop the loop"
    assert row["state_json"]["cursor"] == fibers.FIBER_MAX_OPS_PER_CLAIM, row["state_json"]["cursor"]
    assert (row["lease_owner"], row["lease_until"]) == ("", None), "it yielded without releasing"

    await resume_due_fibers(settings)
    row = await get_fiber(settings, TENANT, fiber_id)
    assert row is not None
    assert row["status"] == "completed", "the rest did not finish on the next sweep"
