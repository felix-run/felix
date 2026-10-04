"""One contract for the skill quality loop's stores -- feedback, evaluations, the tenant policy,
the sweep lock -- run against both backends.

What a dict and a table can quietly disagree on here decides whether work runs once, twice or
never: that a claim is exclusive (two workers asking at once get different rows, or one gets
none) and fair across tenants; that a lapsed claim is taken again and the worker who lost it can
neither heartbeat nor finish; that a job claimed too often is failed; that a version holds one
evaluation in flight (a partial unique index on Postgres); that a decision lands only on pending
feedback; that listings tie-break on the id; and that one sweep runs at a time, by a lease that
lapses, can be taken over, and refuses the holder it was taken from.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from felix.skills import quality_store
from felix.skills.eval_store import get_skill_eval_store
from felix.skills.feedback_store import get_skill_feedback_store
from felix.skills.quality_store import (
    CLAIM_LEASE_MS,
    MAX_ATTEMPTS,
    SkillEvalInFlight,
    SkillFeedbackConflict,
    get_skill_policy_store,
    get_sweep_lease_store,
    sweep_lock,
)

BACKENDS = ["memory", "postgres"]
parametrized = pytest.mark.parametrize("store_settings", BACKENDS, indirect=True)
NOW = 1_000_000_000


def _uuid(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012d}"


def _feedback(
    n: int, *, at: int, name: str = "invoice-triage", source: str = "agent", **kw: Any
) -> dict[str, Any]:
    return {
        "id": _uuid(n),
        "name": name,
        "target_version": "0.1.0",
        "source": source,
        "author": "contributor",
        "body": f"feedback {n}",
        "status": "pending",
        "created_at": at,
        **kw,
    }


def _eval(n: int, *, at: int, version: str = "0.1.0", **kw: Any) -> dict[str, Any]:
    return {
        "id": _uuid(n),
        "name": "invoice-triage",
        "version": version,
        "status": "queued",
        "requested_by": "ops",
        "created_at": at,
        **kw,
    }


async def _accepted(store: Any, tenant: str, n: int, *, at: int | None = None, improve: bool = True) -> None:
    await store.insert(tenant, _feedback(n, at=n if at is None else at))
    await store.decide(tenant, _uuid(n), status="accepted", improve=improve, by="ops", note=None, at=10 + n)


# -- feedback --------------------------------------------------------------------------------


@parametrized
async def test_feedback_round_trips_with_every_column(store_settings: Any) -> None:
    store = get_skill_feedback_store(store_settings)
    await store.insert("acme", _feedback(1, at=5, suggested_patch="use the new limit"))

    row = await store.get("acme", _uuid(1))
    assert row is not None
    assert (row["status"], row["improve"], row["suggested_patch"]) == ("pending", False, "use the new limit")
    assert (row["claim_token"], row["heartbeat_at"], row["claimed_at"], row["attempts"]) == (
        None,
        None,
        None,
        0,
    )
    assert row["result_version"] is None and row["decided_by"] is None
    assert await store.get("globex", _uuid(1)) is None


@parametrized
async def test_a_decision_lands_only_on_pending_feedback(store_settings: Any) -> None:
    store = get_skill_feedback_store(store_settings)
    await store.insert("acme", _feedback(1, at=5))

    decided = await store.decide("acme", _uuid(1), status="accepted", improve=True, by="ops", note=None, at=9)
    assert (decided["status"], decided["improve"], decided["decided_by"], decided["decided_at"]) == (
        "accepted",
        True,
        "ops",
        9,
    )
    with pytest.raises(SkillFeedbackConflict):
        await store.decide("acme", _uuid(1), status="rejected", improve=False, by="ops", note="no", at=10)
    with pytest.raises(SkillFeedbackConflict):
        await store.decide("acme", _uuid(2), status="rejected", improve=False, by="ops", note="no", at=10)


@parametrized
async def test_an_accept_racing_a_reject_has_exactly_one_winner(store_settings: Any) -> None:
    store = get_skill_feedback_store(store_settings)
    await store.insert("acme", _feedback(1, at=5))

    outcomes = await asyncio.gather(
        store.decide("acme", _uuid(1), status="accepted", improve=True, by="a", note=None, at=9),
        store.decide("acme", _uuid(1), status="rejected", improve=False, by="b", note="no", at=9),
        return_exceptions=True,
    )

    conflicts = [o for o in outcomes if isinstance(o, SkillFeedbackConflict)]
    won = [o for o in outcomes if isinstance(o, dict)]
    assert (len(conflicts), len(won)) == (1, 1), outcomes
    row = await store.get("acme", _uuid(1))
    assert row is not None and row["status"] == won[0]["status"]


@parametrized
async def test_feedback_listings_tie_break_on_the_id_and_page(store_settings: Any) -> None:
    store = get_skill_feedback_store(store_settings)
    for n in (3, 1, 2):
        await store.insert("acme", _feedback(n, at=7))  # one millisecond, three rows
    await store.insert("acme", _feedback(4, at=8, name="other-skill"))
    await store.insert("globex", _feedback(5, at=1))

    newest = await store.list_for_skill("acme", "invoice-triage", limit=2)
    assert [r["id"] for r in newest] == [_uuid(3), _uuid(2)]
    rest = await store.list_for_skill("acme", "invoice-triage", before=(7, _uuid(2)))
    assert [r["id"] for r in rest] == [_uuid(1)]

    oldest = await store.list_by_status("acme", "pending", limit=2)
    assert [r["id"] for r in oldest] == [_uuid(1), _uuid(2)]
    after = await store.list_by_status("acme", "pending", after=(7, _uuid(2)))
    assert [r["id"] for r in after] == [_uuid(3), _uuid(4)]


@parametrized
async def test_only_an_agents_pending_feedback_counts_toward_its_cap(store_settings: Any) -> None:
    store = get_skill_feedback_store(store_settings)
    await store.insert("acme", _feedback(1, at=1))
    await store.insert("acme", _feedback(2, at=2))
    await store.insert("acme", _feedback(3, at=3, source="human"))
    await store.insert("acme", _feedback(4, at=4, author="other-agent"))
    await store.decide("acme", _uuid(2), status="rejected", improve=False, by="ops", note="no", at=5)

    assert await store.count_pending_agent("acme", "contributor") == 1
    assert await store.count_pending_agent("globex", "contributor") == 0


@parametrized
async def test_only_accepted_improvements_are_claimed_each_once_across_tenants(store_settings: Any) -> None:
    store = get_skill_feedback_store(store_settings)
    await _accepted(store, "acme", 1)
    await _accepted(store, "acme", 2, improve=False)
    await store.insert("acme", _feedback(3, at=3))  # pending
    await _accepted(store, "globex", 4)

    first, second = await asyncio.gather(store.claim_next(now=NOW), store.claim_next(now=NOW))

    claimed = [r for r in (first, second) if r is not None]
    assert sorted((r["tenant_id"], r["id"]) for r in claimed) == [("acme", _uuid(1)), ("globex", _uuid(4))]
    assert all((r["claimed_at"], r["heartbeat_at"], r["attempts"]) == (NOW, NOW, 1) for r in claimed)
    assert all(r["claim_token"] for r in claimed) and claimed[0]["claim_token"] != claimed[1]["claim_token"]
    assert await store.claim_next(now=NOW + 1) is None


@parametrized
async def test_two_workers_racing_for_one_improvement_get_it_once(store_settings: Any) -> None:
    store = get_skill_feedback_store(store_settings)
    await _accepted(store, "acme", 1)

    outcomes = await asyncio.gather(*(store.claim_next(now=NOW) for _ in range(4)))

    assert [r["id"] for r in outcomes if r is not None] == [_uuid(1)], outcomes


@parametrized
async def test_a_lapsed_improvement_claim_is_retaken_and_the_old_claimer_is_shut_out(
    store_settings: Any,
) -> None:
    store = get_skill_feedback_store(store_settings)
    await _accepted(store, "acme", 1)
    old = await store.claim_next(now=NOW)
    assert old is not None
    assert await store.heartbeat("acme", _uuid(1), token=old["claim_token"], now=NOW + CLAIM_LEASE_MS - 1)
    # Kept alive by the heartbeat: a lease after the claim, it is still held.
    assert await store.claim_next(now=NOW + CLAIM_LEASE_MS) is None

    later = NOW + 3 * CLAIM_LEASE_MS
    new = await store.claim_next(now=later)
    assert new is not None and (new["claimed_at"], new["attempts"]) == (later, 2)

    finish = {"result_version": "0.1.1", "model": "m", "error": None}
    assert not await store.heartbeat("acme", _uuid(1), token=old["claim_token"], now=later)
    assert not await store.finish("acme", _uuid(1), token=old["claim_token"], status="applied", **finish)
    assert await store.finish("acme", _uuid(1), token=new["claim_token"], status="applied", **finish)
    row = await store.get("acme", _uuid(1))
    assert row is not None and (row["status"], row["result_version"], row["claim_token"]) == (
        "applied",
        "0.1.1",
        None,
    )
    assert await store.claim_next(now=later * 2) is None, "applied is final"


@parametrized
async def test_an_improvement_claimed_too_often_is_failed(store_settings: Any) -> None:
    store = get_skill_feedback_store(store_settings)
    await _accepted(store, "acme", 1)
    for n in range(MAX_ATTEMPTS):
        assert await store.claim_next(now=NOW + n * CLAIM_LEASE_MS) is not None

    assert await store.claim_next(now=NOW + MAX_ATTEMPTS * CLAIM_LEASE_MS) is None

    row = await store.get("acme", _uuid(1))
    assert row is not None and (row["status"], row["error"], row["attempts"]) == (
        "failed",
        "attempts_exhausted",
        MAX_ATTEMPTS,
    )


@parametrized
async def test_feedback_job_counts(store_settings: Any) -> None:
    store = get_skill_feedback_store(store_settings)
    await _accepted(store, "acme", 1)  # decided at 11
    await _accepted(store, "acme", 2, improve=False)
    await _accepted(store, "acme", 3)  # decided at 13
    claimed = await store.claim_next(now=NOW)
    assert claimed is not None
    await store.finish(
        "acme",
        claimed["id"],
        token=claimed["claim_token"],
        status="applied",
        result_version="0.1.1",
        model=None,
        error=None,
    )

    assert await store.count_jobs_in_flight("acme") == 1
    assert await store.count_jobs_since("acme", 12) == 1 and await store.count_jobs_since("acme", 0) == 2
    assert await store.count_jobs_in_flight("globex") == 0


# -- evaluations -----------------------------------------------------------------------------


@parametrized
async def test_a_version_holds_one_evaluation_in_flight(store_settings: Any) -> None:
    store = get_skill_eval_store(store_settings)
    await store.insert("acme", _eval(1, at=1))
    with pytest.raises(SkillEvalInFlight):
        await store.insert("acme", _eval(2, at=2))
    # Another version, and another tenant, are not in the way.
    await store.insert("acme", _eval(3, at=3, version="0.1.1"))
    await store.insert("globex", _eval(4, at=4))

    claimed = await store.claim_next(now=NOW)
    assert claimed is not None and claimed["id"] == _uuid(1)
    with pytest.raises(SkillEvalInFlight):
        await store.insert("acme", _eval(5, at=5))  # running is in flight too
    assert await store.finish("acme", _uuid(1), token=claimed["claim_token"], fields={"status": "succeeded"})
    await store.insert("acme", _eval(6, at=6))


@parametrized
async def test_two_workers_never_claim_the_same_evaluation(store_settings: Any) -> None:
    store = get_skill_eval_store(store_settings)
    await store.insert("acme", _eval(1, at=1))
    await store.insert("globex", _eval(2, at=2))

    outcomes = await asyncio.gather(*(store.claim_next(now=NOW) for _ in range(3)))

    claimed = [r for r in outcomes if r is not None]
    assert sorted((r["tenant_id"], r["id"]) for r in claimed) == [("acme", _uuid(1)), ("globex", _uuid(2))]
    assert all((r["status"], r["started_at"], r["attempts"]) == ("running", NOW, 1) for r in claimed)
    assert await store.claim_next(now=NOW + 1) is None


@parametrized
async def test_claims_are_fair_across_tenants(store_settings: Any) -> None:
    """A tenant with a deep queue does not starve one with a single job queued after it."""
    store = get_skill_eval_store(store_settings)
    for n in range(1, 11):
        await store.insert("acme", _eval(n, at=n, version=f"0.1.{n}"))
    await store.insert("globex", _eval(11, at=11))

    first = await store.claim_next(now=NOW)
    second = await store.claim_next(now=NOW + 1)

    assert first is not None and second is not None
    assert [first["tenant_id"], second["tenant_id"]] == ["acme", "globex"]
    assert first["id"] == _uuid(1), "within a tenant, oldest first"
    third = await store.claim_next(now=NOW + 2)
    assert third is not None and (third["tenant_id"], third["id"]) == ("acme", _uuid(2))


@parametrized
async def test_a_lapsed_evaluation_is_retaken_and_only_the_current_claim_writes(store_settings: Any) -> None:
    store = get_skill_eval_store(store_settings)
    await store.insert("acme", _eval(1, at=1))
    old = await store.claim_next(now=NOW)
    assert old is not None
    assert await store.claim_next(now=NOW + CLAIM_LEASE_MS - 1) is None

    later = NOW + CLAIM_LEASE_MS
    retaken = await store.claim_next(now=later)
    assert retaken is not None and (retaken["id"], retaken["started_at"], retaken["attempts"]) == (
        _uuid(1),
        later,
        2,
    )

    done = {"status": "succeeded", "baseline_score": 40, "with_skill_score": 70, "uplift": 30}
    assert not await store.heartbeat("acme", _uuid(1), token=old["claim_token"], now=later)
    assert not await store.finish("acme", _uuid(1), token=old["claim_token"], fields=done)
    scenarios = [{"name": "s", "prompt": "p", "criteria": "c"}]
    assert await store.heartbeat(
        "acme",
        _uuid(1),
        token=retaken["claim_token"],
        now=later + 1,
        fields={"scenario_source": "bundle", "scenarios": scenarios},
    )
    results = [{"name": "s", "baseline_score": 40, "with_skill_score": 70, "uplift": 30}]
    assert await store.finish(
        "acme",
        _uuid(1),
        token=retaken["claim_token"],
        fields={**done, "results": results, "finished_at": later + 5},
    )
    row = await store.get("acme", _uuid(1))
    assert row is not None
    assert (row["status"], row["uplift"], row["results"], row["scenarios"], row["heartbeat_at"]) == (
        "succeeded",
        30,
        results,
        scenarios,
        later + 1,
    )


@parametrized
async def test_an_evaluation_claimed_too_often_is_failed(store_settings: Any) -> None:
    store = get_skill_eval_store(store_settings)
    await store.insert("acme", _eval(1, at=1))
    await store.insert("acme", _eval(2, at=2, version="0.2.0"))
    for n in range(MAX_ATTEMPTS):
        claimed = await store.claim_next(now=NOW + n * CLAIM_LEASE_MS)
        assert claimed is not None and claimed["id"] == _uuid(1)
        await store.finish("acme", _uuid(2), token="not-a-claim", fields={"status": "failed"})  # no effect

    nxt = await store.claim_next(now=NOW + MAX_ATTEMPTS * CLAIM_LEASE_MS)

    assert nxt is not None and nxt["id"] == _uuid(2), "the exhausted job is failed and the next one claimed"
    row = await store.get("acme", _uuid(1))
    assert row is not None and (row["status"], row["error"]) == ("failed", "attempts_exhausted")
    assert row["finished_at"] == NOW + MAX_ATTEMPTS * CLAIM_LEASE_MS


@parametrized
async def test_latest_succeeded_pinned_scenarios_and_listings(store_settings: Any) -> None:
    store = get_skill_eval_store(store_settings)
    first_set = [{"name": "a", "prompt": "p", "criteria": "c"}]
    for n, uplift, source in ((1, 5, "generated"), (2, 9, "bundle"), (3, 7, "generated")):
        await store.insert("acme", _eval(n, at=n))
        claimed = await store.claim_next(now=NOW + n)
        assert claimed is not None
        fields = {
            "status": "succeeded",
            "uplift": uplift,
            "scenario_source": source,
            "finished_at": NOW + 10 * n,
        }
        await store.finish(
            "acme", _uuid(n), token=claimed["claim_token"], fields={**fields, "scenarios": first_set}
        )
    await store.insert("acme", _eval(4, at=4))
    claimed = await store.claim_next(now=NOW)
    assert claimed is not None
    await store.finish(
        "acme", _uuid(4), token=claimed["claim_token"], fields={"status": "failed", "finished_at": NOW + 99}
    )

    latest = await store.latest_succeeded("acme", "invoice-triage", "0.1.0")
    assert latest is not None and (latest["id"], latest["uplift"]) == (_uuid(3), 7)
    bundled = await store.latest_succeeded("acme", "invoice-triage", "0.1.0", scenario_source="bundle")
    assert bundled is not None and bundled["id"] == _uuid(2)
    assert await store.latest_succeeded("acme", "invoice-triage", "9.9.9") is None
    assert await store.latest_succeeded("globex", "invoice-triage", "0.1.0") is None
    assert await store.pinned_scenarios("acme", "invoice-triage", "0.1.0") == {
        "scenario_source": "generated",
        "scenarios": first_set,
    }
    assert await store.pinned_scenarios("acme", "invoice-triage", "9.9.9") is None

    listed = await store.list_for_skill("acme", "invoice-triage", limit=2)
    assert [r["id"] for r in listed] == [_uuid(4), _uuid(3)]
    rest = await store.list_for_skill("acme", "invoice-triage", before=(3, _uuid(3)))
    assert [r["id"] for r in rest] == [_uuid(2), _uuid(1)]
    assert await store.list_for_skill("acme", "invoice-triage", version="0.2.0") == []


@parametrized
async def test_evaluation_job_counts(store_settings: Any) -> None:
    store = get_skill_eval_store(store_settings)
    await store.insert("acme", _eval(1, at=100))
    await store.insert("acme", _eval(2, at=200, version="0.2.0"))
    claimed = await store.claim_next(now=NOW)
    assert claimed is not None
    await store.finish("acme", claimed["id"], token=claimed["claim_token"], fields={"status": "succeeded"})

    assert await store.count_jobs_in_flight("acme") == 1
    assert (await store.count_jobs_since("acme", 150), await store.count_jobs_since("acme", 0)) == (1, 2)
    assert await store.count_jobs_in_flight("globex") == 0


# -- policy and the sweep lock -----------------------------------------------------------------


@parametrized
async def test_the_policy_row_is_replaced_whole_tenant_scoped_and_deletable(store_settings: Any) -> None:
    store = get_skill_policy_store(store_settings)
    assert await store.get("acme") is None
    row = {
        "min_quality": 60,
        "block_on_advisory": True,
        "require_eval": True,
        "min_eval_uplift": 5,
        "import_min_age_days": 14,
        "updated_at": 1,
        "updated_by": "ops",
    }
    await store.put("acme", row)
    stored = await store.put("acme", {**row, "min_eval_uplift": None, "updated_at": 2})

    assert stored["min_eval_uplift"] is None and stored["updated_at"] == 2
    got = await store.get("acme")
    assert got is not None and {k: got[k] for k in row} == {**row, "min_eval_uplift": None, "updated_at": 2}
    assert await store.get("globex") is None
    assert await store.delete("globex") is False
    assert await store.delete("acme") is True and await store.get("acme") is None


@parametrized
async def test_one_sweep_holds_the_lock_at_a_time(store_settings: Any) -> None:
    async with sweep_lock(store_settings) as first, sweep_lock(store_settings) as second:
        assert first is not None and second is None
        assert await first.renew(), "the holder renews"
    async with sweep_lock(store_settings) as again:
        assert again is not None, "released when the sweep ends"


LEASE = 60_000


@parametrized
async def test_two_acquirers_racing_for_the_sweep_lease_get_one_winner(store_settings: Any) -> None:
    """Racing on an absent row (the first sweep after the upgrade) and on a lapsed one."""
    lease = get_sweep_lease_store(store_settings)
    tokens = [f"t{n}" for n in range(4)]

    won = await asyncio.gather(*(lease.acquire(t, now=NOW, lease_ms=LEASE) for t in tokens))
    assert sum(won) == 1, won
    assert not await lease.acquire("late", now=NOW + LEASE, lease_ms=LEASE), "held through `until`"

    retaken = await asyncio.gather(*(lease.acquire(t, now=NOW + LEASE + 1, lease_ms=LEASE) for t in tokens))
    assert sum(retaken) == 1, retaken


@parametrized
async def test_a_lapsed_sweep_lease_is_taken_over_and_the_evicted_holder_is_refused(
    store_settings: Any,
) -> None:
    lease = get_sweep_lease_store(store_settings)
    assert await lease.acquire("old", now=NOW, lease_ms=LEASE)
    assert await lease.acquire("old", now=NOW + 1, lease_ms=LEASE), "the holder takes it again"
    assert await lease.renew("old", now=NOW + 2, lease_ms=LEASE)
    assert not await lease.acquire("new", now=NOW + 2 + LEASE, lease_ms=LEASE), "renewed, so still held"

    taken = NOW + 3 + LEASE
    assert await lease.acquire("new", now=taken, lease_ms=LEASE), "lapsed, so taken over"
    assert not await lease.renew("old", now=taken, lease_ms=LEASE)
    assert not await lease.release("old"), "an evicted holder cannot free the new holder's lease"
    assert not await lease.acquire("other", now=taken + 1, lease_ms=LEASE)

    assert await lease.release("new")
    assert await lease.acquire("other", now=taken + 2, lease_ms=LEASE), "released, so free at once"


@parametrized
async def test_the_tenant_scan_is_cut_after_ordering_by_last_claim(
    store_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the scan cut to one tenant, the tenant never claimed from is the one scanned -- not
    whichever sorts first by id, which would be claimed from every time while `zeta` waits."""
    monkeypatch.setattr(quality_store, "_TENANTS_SCANNED", 1)
    store = get_skill_eval_store(store_settings)
    for n in range(1, 4):
        await store.insert("acme", _eval(n, at=n, version=f"0.1.{n}"))
    await store.insert("zeta", _eval(9, at=9))

    first = await store.claim_next(now=NOW)
    second = await store.claim_next(now=NOW + 1)
    third = await store.claim_next(now=NOW + 2)

    assert first is not None and second is not None and third is not None
    assert [first["tenant_id"], second["tenant_id"], third["tenant_id"]] == ["acme", "zeta", "acme"]
