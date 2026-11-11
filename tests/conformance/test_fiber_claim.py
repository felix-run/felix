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
async def test_a_pending_fiber_is_claimed_and_marked_running(fiber_settings: Any) -> None:
    created = await fibers.create_fiber(fiber_settings, TENANT, kind="step", status="pending")

    claimed = await _claim(fiber_settings)

    assert [row["id"] for row in claimed] == [created["id"]], claimed
    assert claimed[0]["status"] == "running"
    stored = await fibers.get_fiber(fiber_settings, TENANT, created["id"])
    assert stored is not None and stored["status"] == "running"


@parametrized
async def test_a_sleeping_fiber_is_not_due_until_its_timer_fires(fiber_settings: Any) -> None:
    """A sleeping fiber claimed early runs before the delay the caller asked for."""
    now = fibers.now_ms()
    created = await fibers.create_fiber(fiber_settings, TENANT, status="sleeping", wake_at=now + 60_000)

    assert await _claim(fiber_settings, ts=now) == []

    woken = await _claim(fiber_settings, ts=now + 60_001)
    assert [row["id"] for row in woken] == [created["id"]], woken
    assert woken[0]["wake_at"] is None, "claiming a woken fiber must clear its timer"


@parametrized
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
async def test_claiming_takes_a_lease_that_blocks_a_second_claim(fiber_settings: Any) -> None:
    """The lease is what stops two replicas running one step twice."""
    created = await fibers.create_fiber(fiber_settings, TENANT, status="pending")

    first = await _claim(fiber_settings)
    assert [row["id"] for row in first] == [created["id"]]
    assert first[0]["lease_until"], "a claim must take a lease"

    assert await _claim(fiber_settings) == [], "a leased fiber must not be claimed again"


@parametrized
async def test_an_expired_lease_is_reclaimable(fiber_settings: Any) -> None:
    """A worker that crashed mid-step must not strand its fiber forever."""
    created = await fibers.create_fiber(fiber_settings, TENANT, status="pending")
    await _claim(fiber_settings)

    later = fibers.now_ms() + fibers.FIBER_LEASE_MS + 1
    reclaimed = await _claim(fiber_settings, ts=later)
    assert [row["id"] for row in reclaimed] == [created["id"]], reclaimed


@parametrized
async def test_the_claim_records_who_holds_it(fiber_settings: Any) -> None:
    """`lease_owner` is how an operator tells a stuck fiber from a busy one."""
    await fibers.create_fiber(fiber_settings, TENANT, status="pending")

    claimed = await _claim(fiber_settings)
    assert claimed[0]["lease_owner"], claimed


@parametrized
async def test_a_renewal_says_whether_the_claim_is_still_held(fiber_settings: Any) -> None:
    """felix-run/felix#531. The heartbeat stops its step on `False`; on Postgres that answer is
    the update's rowcount, which the memory twin cannot stand in for. A renewal that matched
    no row used to look exactly like one that did."""
    await fibers.create_fiber(fiber_settings, TENANT, status="pending")
    (mine,) = await _claim(fiber_settings)
    assert await fibers._renew_lease(fiber_settings, mine) is True

    # Lapsed and taken by another worker.
    other = fiber_settings.model_copy(update={"replica_id": "worker-b"})
    (theirs,) = await _claim(other, ts=fibers.now_ms() + fibers.FIBER_LEASE_MS + 1)
    assert theirs["id"] == mine["id"]

    assert await fibers._renew_lease(fiber_settings, mine) is False
    assert await fibers._renew_lease(other, theirs) is True


# --- fibers an earlier version handed to Temporal -----------------------------------------


@parametrized
async def test_a_fiber_an_earlier_version_gave_to_temporal_is_claimed(fiber_settings: Any) -> None:
    """The Temporal backend is gone, so nothing else will ever drive a row it was given.

    Its state is all in the row, so this scheduler picks it up; skipping it, as both arms did
    while Temporal existed, would strand every in-flight durable chat across the upgrade.
    """
    stranded = await fibers.create_fiber(
        fiber_settings, TENANT, status="pending", state={"backend": "temporal"}
    )

    claimed = await _claim(fiber_settings)

    assert [row["id"] for row in claimed] == [stranded["id"]]


# --- batching and order ---------------------------------------------------------------------


@parametrized
async def test_a_claim_is_bounded_by_the_batch_size(fiber_settings: Any) -> None:
    """An unbounded claim leases every fiber in the tenant to one tick."""
    for _ in range(fibers.FIBER_BATCH + 5):
        await fibers.create_fiber(fiber_settings, TENANT, status="pending")

    claimed = await _claim(fiber_settings)
    assert len(claimed) == fibers.FIBER_BATCH, len(claimed)


@parametrized
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
async def test_claiming_advances_the_field_the_queue_is_ordered_by(fiber_settings: Any) -> None:
    """Fairness, and the reason `updated_at` is not just bookkeeping.

    The claim orders by `updated_at`, so advancing it on claim is what turns the queue into
    round-robin: a fiber that has just run sorts behind everything waiting. The twin did not
    advance it, so there one fiber could be re-picked ahead of the rest indefinitely.

    Asserted on the single row rather than by comparing two claimed fibers. An earlier version
    claimed a pair and expected their order to survive — but a batch claim stamps every row it
    takes with the *same* `ts`, so the two then tie and Postgres resolves the tie arbitrarily.
    That version passed on memory and failed in CI against a real database, which is the
    divergence direction this file is least able to see from here.
    """
    created = await fibers.create_fiber(fiber_settings, TENANT, status="pending")

    # An explicit later instant rather than "now": creating and claiming inside one millisecond
    # leaves `updated_at` equal, and the assertion would then be about clock resolution.
    later = fibers.now_ms() + 1000
    claimed = await _claim(fiber_settings, ts=later)

    assert [row["id"] for row in claimed] == [created["id"]]
    assert claimed[0]["updated_at"] == later, (created, claimed)
    assert claimed[0]["version"] > int(created["version"] or 0), (created, claimed)


# --- one claim is not the next one ------------------------------------------------------------


async def _lapsed_then_reclaimed(settings: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """A claim nobody renewed, and the claim the *same* process took once it had lapsed.

    A worker claims from two places, its loop and the cron sweep, under one `lease_owner`.
    """
    await fibers.create_fiber(settings, TENANT, status="pending")
    t0 = fibers.now_ms()
    (stale,) = await _claim(settings, ts=t0)
    (live,) = await _claim(settings, ts=t0 + fibers.FIBER_LEASE_MS + 1)
    assert live["lease_owner"] == stale["lease_owner"] and live["lease_until"] > stale["lease_until"]
    return stale, live


async def _lease(settings: Any, row: dict[str, Any]) -> tuple[str, int | None]:
    stored = await fibers.get_fiber(settings, TENANT, row["id"])
    assert stored is not None
    return stored["lease_owner"], stored["lease_until"]


@parametrized
async def test_a_lapsed_claims_holder_cannot_release_the_next_claim(fiber_settings: Any) -> None:
    """Scoped to the worker, the release matched any claim that worker held: the stale holder
    cleared a lease a step was running under, and the fiber was free for a third claim."""
    stale, live = await _lapsed_then_reclaimed(fiber_settings)

    await fibers._release_fiber(fiber_settings, stale)
    assert await _lease(fiber_settings, live) == (live["lease_owner"], live["lease_until"])

    await fibers._release_fiber(fiber_settings, live)
    assert await _lease(fiber_settings, live) == ("", None), "the holder itself could not release"


@parametrized
async def test_a_lapsed_claims_holder_cannot_renew_the_next_claim(fiber_settings: Any) -> None:
    """A stale holder told `True` keeps its step running beside the live one."""
    stale, live = await _lapsed_then_reclaimed(fiber_settings)

    assert await fibers._renew_lease(fiber_settings, stale) is False
    assert await _lease(fiber_settings, live) == (live["lease_owner"], live["lease_until"])
    assert await fibers._renew_lease(fiber_settings, live) is True


@parametrized
async def test_a_lapsed_claims_holder_cannot_park_the_next_claim(fiber_settings: Any) -> None:
    """`_record_attempt` writes `status` and clears the lease with no compare-and-set."""
    stale, live = await _lapsed_then_reclaimed(fiber_settings)

    await fibers._record_attempt(fiber_settings, {**stale, "status": "sleeping", "attempts": 3})
    stored = await fibers.get_fiber(fiber_settings, TENANT, live["id"])
    assert stored is not None
    assert (stored["status"], stored["attempts"], stored["lease_until"]) == (
        "running",
        0,
        live["lease_until"],
    )

    await fibers._record_attempt(fiber_settings, {**live, "status": "sleeping", "attempts": 3})
    stored = await fibers.get_fiber(fiber_settings, TENANT, live["id"])
    assert stored is not None
    assert (stored["status"], stored["attempts"], stored["lease_until"]) == ("sleeping", 3, None)


@parametrized
async def test_a_lapsed_claims_holder_is_told_before_it_does_work(fiber_settings: Any) -> None:
    """The write a step makes before an `invoke`: refused, the row is someone else's."""
    stale, live = await _lapsed_then_reclaimed(fiber_settings)

    assert await fibers._checkpoint_state(fiber_settings, stale) is False
    assert await fibers._checkpoint_state(fiber_settings, live) is True


@parametrized
async def test_a_save_that_keeps_the_claim_leaves_the_lease_to_the_heartbeat(
    fiber_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A step's save wrote back the `lease_until` it was claimed with, undoing every renewal
    since: a long claim's closing save could leave a `running` row whose lease was already past."""
    await fibers.create_fiber(fiber_settings, TENANT, status="pending")
    t0 = fibers.now_ms()
    (mine,) = await _claim(fiber_settings, ts=t0)
    monkeypatch.setattr(fibers, "now_ms", lambda: t0 + fibers.FIBER_LEASE_RENEW_MS)
    # The heartbeat's renewal, which the step's own copy of the row has not seen.
    assert await fibers._renew_lease(fiber_settings, dict(mine)) is True
    renewed = t0 + fibers.FIBER_LEASE_RENEW_MS + fibers.FIBER_LEASE_MS

    await fibers._save_fiber(fiber_settings, mine, hold_claim=True)

    assert await _lease(fiber_settings, mine) == (mine["lease_owner"], renewed)
    await fibers._save_fiber(fiber_settings, mine)
    assert await _lease(fiber_settings, mine) == ("", None), "an ordinary save releases the claim"


# --- a holder that never let go ---------------------------------------------------------------


def _lapses(row: dict[str, Any]) -> int:
    return int((row["state_json"] or {}).get(fibers.FIBER_LAPSES_KEY) or 0)


@parametrized
async def test_a_fiber_whose_worker_was_lost_is_charged_and_then_buried(fiber_settings: Any) -> None:
    """A step that kills its worker raises nothing, so only the claim can count it."""
    settings = fiber_settings.model_copy(update={"fiber_max_attempts": 2})
    created = await fibers.create_fiber(settings, TENANT, status="pending")
    t0 = fibers.now_ms()
    lapse = fibers.FIBER_LEASE_MS + 1

    (first,) = await _claim(settings, ts=t0)
    (second,) = await _claim(settings, ts=t0 + lapse)
    assert (_lapses(first), _lapses(second), second["status"]) == (0, 1, "running")

    (buried,) = await _claim(settings, ts=t0 + 2 * lapse)
    assert (buried["status"], buried["lease_owner"], buried["lease_until"]) == ("dead", "", None)
    stored = await fibers.get_fiber(settings, TENANT, created["id"])
    assert stored is not None
    assert (stored["status"], stored["lease_owner"], stored["lease_until"]) == ("dead", "", None)
    assert stored["state_json"]["stash"]["last"]["error"] == "step lost its worker 2 times; not run again"
    assert stored["version"] > second["version"], "a holder still out there could write over the burial"
    assert await _claim(settings, ts=t0 + 3 * lapse) == []


@parametrized
@pytest.mark.parametrize("status", ["pending", "sleeping", "running"])
async def test_a_fiber_nobody_was_holding_is_not_charged(fiber_settings: Any, status: str) -> None:
    """Queued, asleep, or released between steps: no worker was lost on any of them."""
    settings = fiber_settings.model_copy(update={"fiber_max_attempts": 1})
    now = fibers.now_ms()
    await fibers.create_fiber(
        settings, TENANT, status=status, wake_at=now + 1000 if status == "sleeping" else None
    )

    (claimed,) = await _claim(settings, ts=now + fibers.FIBER_LEASE_MS + 1)

    assert (claimed["status"], _lapses(claimed)) == ("running", 0)


@parametrized
async def test_a_claim_that_was_released_ends_the_streak(fiber_settings: Any) -> None:
    settings = fiber_settings.model_copy(update={"fiber_max_attempts": 3})
    await fibers.create_fiber(settings, TENANT, status="pending")
    t0 = fibers.now_ms()
    await _claim(settings, ts=t0)
    (lost,) = await _claim(settings, ts=t0 + fibers.FIBER_LEASE_MS + 1)
    assert _lapses(lost) == 1

    await fibers._release_fiber(settings, lost)
    (again,) = await _claim(settings, ts=t0 + fibers.FIBER_LEASE_MS + 2)

    assert _lapses(again) == 0
    stored = await fibers.get_fiber(settings, TENANT, again["id"])
    assert stored is not None and _lapses(stored) == 0


# --- what a sweep is over ---------------------------------------------------------------------


@parametrized
async def test_the_due_list_names_what_a_claim_would_take_and_claims_nothing(fiber_settings: Any) -> None:
    import asyncio

    first = await fibers.create_fiber(fiber_settings, TENANT, status="pending")
    await asyncio.sleep(0.01)
    second = await fibers.create_fiber(fiber_settings, "other", status="pending")
    await fibers.create_fiber(fiber_settings, TENANT, status="sleeping", wake_at=fibers.now_ms() + 60_000)
    done = await fibers.create_fiber(fiber_settings, TENANT, status="pending")
    await fibers._save_fiber(fiber_settings, {**done, "status": "completed"})

    due = await fibers._due_keys(fiber_settings)

    assert due == [(TENANT, first["id"]), ("other", second["id"])]
    assert await fibers._due_keys(fiber_settings, 1) == [(TENANT, first["id"])]
    stored = await fibers.get_fiber(fiber_settings, TENANT, first["id"])
    assert stored is not None and (stored["status"], stored.get("lease_until")) == ("pending", None)


@parametrized
async def test_a_claim_narrowed_to_some_fibers_takes_no_others(fiber_settings: Any) -> None:
    """How a sweep leaves a fiber that arrived after it began to the next one."""
    from felix.db.session import _use_memory

    await fibers.create_fiber(fiber_settings, TENANT, status="pending")
    wanted = await fibers.create_fiber(fiber_settings, "other", status="pending")
    only = frozenset({("other", wanted["id"]), (TENANT, wanted["id"]), ("other", "no-such-fiber")})
    arm = fibers._claim_due_memory if _use_memory(fiber_settings) else fibers._claim_due_postgres

    claimed = await arm(fiber_settings, fibers.now_ms(), fibers.FIBER_BATCH, only)
    assert [(row["tenant_id"], row["id"]) for row in claimed] == [("other", wanted["id"])]

    assert await arm(fiber_settings, fibers.now_ms(), fibers.FIBER_BATCH, frozenset()) == []
    assert len(await arm(fiber_settings, fibers.now_ms(), fibers.FIBER_BATCH, None)) == 1
