"""One contract for the fiber *claim* path, run against both backends.

`test_fiber_store.py` covers the retry count through backoff and burial. This covers the step
before that: which fibers a scheduler tick picks up, and what claiming one does to the row.

That path is where the two implementations are least alike. Postgres selects with
`ORDER BY updated_at ... LIMIT ... FOR UPDATE SKIP LOCKED` and then filters in Python; the twin
scans a dict in insertion order and filters as it goes. The lease is what stops the same step
running twice across replicas, so a divergence here is not a reporting difference — it decides
whether work runs, runs twice, or never runs at all.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.durability import fibers

BACKENDS = ["memory", "postgres"]
parametrized = pytest.mark.parametrize("fiber_settings", BACKENDS, indirect=True)
TENANT = "conformance"


async def _claim(settings: Any, ts: int | None = None) -> list[dict[str, Any]]:
    """Claim exactly the way `resume_due_fibers` does, without running the steps."""
    from felix.db.session import _use_memory

    at = fibers.now_ms() if ts is None else ts
    if _use_memory(settings):
        return await fibers._claim_due_memory(settings, at)
    return await fibers._claim_due_postgres(settings, at)


# --- what is due ----------------------------------------------------------------------------


@parametrized
@pytest.mark.asyncio
async def test_a_pending_fiber_is_claimed_and_marked_running(fiber_settings: Any) -> None:
    created = await fibers.create_fiber(fiber_settings, TENANT, kind="step", status="pending")

    claimed = await _claim(fiber_settings)

    assert [row["id"] for row in claimed] == [created["id"]], claimed
    assert claimed[0]["status"] == "running"
    stored = await fibers.get_fiber(fiber_settings, TENANT, created["id"])
    assert stored is not None and stored["status"] == "running"


@parametrized
@pytest.mark.asyncio
async def test_a_sleeping_fiber_is_not_due_until_its_timer_fires(fiber_settings: Any) -> None:
    """A sleeping fiber claimed early runs before the delay the caller asked for."""
    now = fibers.now_ms()
    created = await fibers.create_fiber(fiber_settings, TENANT, status="sleeping", wake_at=now + 60_000)

    assert await _claim(fiber_settings, ts=now) == []

    woken = await _claim(fiber_settings, ts=now + 60_001)
    assert [row["id"] for row in woken] == [created["id"]], woken
    assert woken[0]["wake_at"] is None, "claiming a woken fiber must clear its timer"


@parametrized
@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", sorted(fibers.FIBER_TERMINAL_STATUSES))
async def test_a_terminal_fiber_is_never_claimed(fiber_settings: Any, terminal: str) -> None:
    """Parametrized over the real terminal set rather than one arbitrary string.

    An earlier version used `status="done"`, which is not in `FIBER_TERMINAL_STATUSES` at all —
    it passed because the claim's `status IN (...)` excludes any unrecognised value, so the
    test would have passed just as well with `status="banana"` and said nothing about the
    statuses that actually mean finished.
    """
    done = await fibers.create_fiber(fiber_settings, TENANT, status="pending")
    await fibers._save_fiber(fiber_settings, {**done, "status": terminal})

    assert await _claim(fiber_settings) == []


# --- the lease ------------------------------------------------------------------------------


@parametrized
@pytest.mark.asyncio
async def test_claiming_takes_a_lease_that_blocks_a_second_claim(fiber_settings: Any) -> None:
    """The lease is what stops two replicas running one step twice."""
    created = await fibers.create_fiber(fiber_settings, TENANT, status="pending")

    first = await _claim(fiber_settings)
    assert [row["id"] for row in first] == [created["id"]]
    assert first[0]["lease_until"], "a claim must take a lease"

    assert await _claim(fiber_settings) == [], "a leased fiber must not be claimed again"


@parametrized
@pytest.mark.asyncio
async def test_an_expired_lease_is_reclaimable(fiber_settings: Any) -> None:
    """A worker that crashed mid-step must not strand its fiber forever."""
    created = await fibers.create_fiber(fiber_settings, TENANT, status="pending")
    await _claim(fiber_settings)

    later = fibers.now_ms() + fibers.FIBER_LEASE_MS + 1
    reclaimed = await _claim(fiber_settings, ts=later)
    assert [row["id"] for row in reclaimed] == [created["id"]], reclaimed


@parametrized
@pytest.mark.asyncio
async def test_the_claim_records_who_holds_it(fiber_settings: Any) -> None:
    """`lease_owner` is how an operator tells a stuck fiber from a busy one."""
    await fibers.create_fiber(fiber_settings, TENANT, status="pending")

    claimed = await _claim(fiber_settings)
    assert claimed[0]["lease_owner"], claimed


# --- fibers this scheduler does not own -----------------------------------------------------


@parametrized
@pytest.mark.asyncio
async def test_a_temporal_backed_fiber_is_never_claimed(fiber_settings: Any) -> None:
    """Temporal drives its own workflows; claiming one here would run the step twice."""
    await fibers.create_fiber(fiber_settings, TENANT, status="pending", state={"backend": "temporal"})

    assert await _claim(fiber_settings) == []


@parametrized
@pytest.mark.asyncio
async def test_temporal_fibers_do_not_starve_the_batch(fiber_settings: Any) -> None:
    """A batch full of Temporal rows must not stop the scheduler seeing real work.

    The two arms filter at different points. The twin skips Temporal rows *before* counting
    toward `FIBER_BATCH`, so it scans past them. Postgres applies `LIMIT FIBER_BATCH` in SQL and
    only then drops Temporal rows in Python, so a tenant holding a batch's worth of
    Temporal-backed fibers claims nothing at all and its ordinary fibers never run.

    That is starvation on the system of record and not on the twin, which is the shape a
    conformance suite exists to surface.
    """
    # Comfortably more than one batch, so the real fiber is excluded by the limit rather than
    # by a tie: at exactly `FIBER_BATCH` the timestamps can collide, and which row falls outside
    # the window is then Postgres's arbitrary choice among equal sort keys.
    for _ in range(fibers.FIBER_BATCH + 5):
        await fibers.create_fiber(fiber_settings, TENANT, status="pending", state={"backend": "temporal"})
    real = await fibers.create_fiber(fiber_settings, TENANT, status="pending")

    claimed = await _claim(fiber_settings)

    assert [row["id"] for row in claimed] == [real["id"]], (
        "the ordinary fiber is starved behind a batch of Temporal rows"
    )


# --- batching and order ---------------------------------------------------------------------


@parametrized
@pytest.mark.asyncio
async def test_a_claim_is_bounded_by_the_batch_size(fiber_settings: Any) -> None:
    """An unbounded claim leases every fiber in the tenant to one tick."""
    for _ in range(fibers.FIBER_BATCH + 5):
        await fibers.create_fiber(fiber_settings, TENANT, status="pending")

    claimed = await _claim(fiber_settings)
    assert len(claimed) == fibers.FIBER_BATCH, len(claimed)


@parametrized
@pytest.mark.asyncio
async def test_the_oldest_fiber_is_claimed_first(fiber_settings: Any) -> None:
    """Postgres orders by `updated_at`; the twin scans insertion order.

    With more due fibers than one batch holds, the two arms otherwise pick different work, and
    a fiber that keeps losing the race is one that never runs. Oldest-first is the answer that
    makes the wait bounded.
    """
    import asyncio

    first = await fibers.create_fiber(fiber_settings, TENANT, status="pending")
    await asyncio.sleep(0.01)
    second = await fibers.create_fiber(fiber_settings, TENANT, status="pending")

    # A batch of one, so the ordering decides which is taken rather than merely their order.
    claimed = (await _claim(fiber_settings))[:1]
    assert [row["id"] for row in claimed] == [first["id"]], (first["id"], second["id"], claimed)


# --- tenancy --------------------------------------------------------------------------------


@parametrized
@pytest.mark.asyncio
async def test_the_sweep_claims_across_tenants(fiber_settings: Any) -> None:
    """Deliberately not tenant-scoped: this is cross-tenant maintenance like retention.

    Pinned because it is the opposite of the rule everywhere else in the store layer, and a
    reader who assumed tenant scoping would "fix" it into a scheduler that stalls every tenant
    but one.

    It does *not* exercise the `rls_bypass()` the claim wraps itself in: the conformance
    database connects as a superuser with `FELIX_DATABASE_RLS` unset, so every transaction
    already bypasses and the wrap is inert here. Under an enforcing role `create_fiber` cannot
    even insert — it is the one write in the module that neither bypasses nor binds the tenant
    GUC — which is tracked in `docs/ROADMAP.md` rather than fixed here.
    """
    mine = await fibers.create_fiber(fiber_settings, TENANT, status="pending")
    theirs = await fibers.create_fiber(fiber_settings, "other", status="pending")

    claimed = {row["id"] for row in await _claim(fiber_settings)}
    assert claimed == {mine["id"], theirs["id"]}, claimed


@parametrized
@pytest.mark.asyncio
async def test_claiming_moves_a_fiber_to_the_back_of_the_queue(fiber_settings: Any) -> None:
    """Fairness, and the reason `updated_at` is not just bookkeeping.

    The claim orders by `updated_at`, so advancing it on claim is what turns the queue into
    round-robin. Postgres did this and the twin did not, so on the twin a re-claimed fiber
    stayed at the front and could be picked ahead of everything else indefinitely — one fiber
    monopolising a scheduler that on the system of record would have shared it out.
    """
    import asyncio

    first = await fibers.create_fiber(fiber_settings, TENANT, status="pending")
    await asyncio.sleep(0.01)
    second = await fibers.create_fiber(fiber_settings, TENANT, status="pending")

    claimed = await _claim(fiber_settings)
    assert {row["id"] for row in claimed} == {first["id"], second["id"]}

    # Both leases expire together; the one claimed first must now sort last.
    later = fibers.now_ms() + fibers.FIBER_LEASE_MS + 1
    again = await _claim(fiber_settings, ts=later)
    assert again[0]["id"] == first["id"], (
        "the fiber claimed first is the oldest by updated_at and must come first again"
    )
    assert all(row["version"] >= 1 for row in again), again
