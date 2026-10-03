"""One contract for the skill quality loop's stores -- feedback, evaluations, the tenant policy --
run against both backends.

What a dict and a table can quietly disagree on here decides whether work runs once, twice or
never: that a claim is exclusive (two workers asking at once get different rows, or one gets
none), that a lapsed claim is taken again and the worker who lost it cannot write over the one
who took it, that a version holds one evaluation in flight (a partial unique index on Postgres),
that a decision lands only on pending feedback, and that listings tie-break on the id so the
two arms agree on where a page ends.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from felix.skills.quality_store import (
    CLAIM_LEASE_MS,
    SkillEvalInFlight,
    SkillFeedbackConflict,
    get_skill_eval_store,
    get_skill_feedback_store,
    get_skill_policy_store,
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


# -- feedback --------------------------------------------------------------------------------


@parametrized
async def test_feedback_round_trips_with_every_column(store_settings: Any) -> None:
    store = get_skill_feedback_store(store_settings)
    await store.insert("acme", _feedback(1, at=5, suggested_patch="use the new limit"))

    row = await store.get("acme", _uuid(1))
    assert row is not None
    assert (row["status"], row["improve"], row["suggested_patch"]) == ("pending", False, "use the new limit")
    assert row["result_version"] is None and row["claimed_at"] is None and row["decided_by"] is None
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
async def test_only_accepted_improvements_are_claimed_and_each_by_one_worker(store_settings: Any) -> None:
    store = get_skill_feedback_store(store_settings)
    for n in (1, 2, 3):
        await store.insert("acme", _feedback(n, at=n))
    await store.insert("globex", _feedback(4, at=4))
    await store.decide("acme", _uuid(1), status="accepted", improve=True, by="ops", note=None, at=10)
    await store.decide("acme", _uuid(2), status="accepted", improve=False, by="ops", note=None, at=10)
    await store.decide("globex", _uuid(4), status="accepted", improve=True, by="ops", note=None, at=10)

    first, second = await asyncio.gather(
        store.claim_improvements(limit=1, now=NOW), store.claim_improvements(limit=1, now=NOW)
    )
    claimed = [r["id"] for r in first + second]
    # Across tenants, without and accept-without-improve, never twice.
    assert sorted(claimed) == [_uuid(1), _uuid(4)], claimed
    assert all(r["claimed_at"] == NOW for r in first + second)
    assert await store.claim_improvements(limit=5, now=NOW + 1) == []
    assert await store.claim_improvement("acme", _uuid(2), now=NOW) is None


@parametrized
async def test_a_lapsed_improvement_claim_is_retaken_and_the_old_claimer_cannot_finish(
    store_settings: Any,
) -> None:
    store = get_skill_feedback_store(store_settings)
    await store.insert("acme", _feedback(1, at=1))
    await store.decide("acme", _uuid(1), status="accepted", improve=True, by="ops", note=None, at=2)
    old = await store.claim_improvement("acme", _uuid(1), now=NOW)
    assert old is not None

    later = NOW + CLAIM_LEASE_MS
    new = await store.claim_improvement("acme", _uuid(1), now=later)
    assert new is not None and new["claimed_at"] == later

    finish = {"result_version": "0.1.1", "model": "m", "error": None}
    assert not await store.finish_improvement("acme", _uuid(1), claimed_at=NOW, status="applied", **finish)
    assert await store.finish_improvement("acme", _uuid(1), claimed_at=later, status="applied", **finish)
    row = await store.get("acme", _uuid(1))
    assert row is not None and (row["status"], row["result_version"]) == ("applied", "0.1.1")
    # Applied is final: nothing claims it again, however late.
    assert await store.claim_improvement("acme", _uuid(1), now=later * 2) is None


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

    claimed = await store.claim("acme", _uuid(1), now=NOW)
    assert claimed is not None
    with pytest.raises(SkillEvalInFlight):
        await store.insert("acme", _eval(5, at=5))  # running is in flight too
    assert await store.finish("acme", _uuid(1), started_at=NOW, fields={"status": "succeeded", "uplift": 4})
    await store.insert("acme", _eval(6, at=6))


@parametrized
async def test_two_workers_never_claim_the_same_evaluation(store_settings: Any) -> None:
    store = get_skill_eval_store(store_settings)
    await store.insert("acme", _eval(1, at=1))
    await store.insert("globex", _eval(2, at=2))

    first, second = await asyncio.gather(
        store.claim_queued(limit=1, now=NOW), store.claim_queued(limit=1, now=NOW)
    )
    claimed = [r["id"] for r in first + second]
    assert sorted(claimed) == [_uuid(1), _uuid(2)], claimed
    assert all((r["status"], r["started_at"]) == ("running", NOW) for r in first + second)
    assert await store.claim_queued(limit=5, now=NOW + 1) == []

    await store.insert("acme", _eval(3, at=3, version="0.2.0"))
    one, other = await asyncio.gather(
        store.claim("acme", _uuid(3), now=NOW), store.claim("acme", _uuid(3), now=NOW)
    )
    assert [r for r in (one, other) if r is not None] != [] and (one is None) != (other is None)


@parametrized
async def test_a_lapsed_evaluation_is_retaken_and_only_the_current_claim_finishes(
    store_settings: Any,
) -> None:
    store = get_skill_eval_store(store_settings)
    await store.insert("acme", _eval(1, at=1))
    assert await store.claim("acme", _uuid(1), now=NOW) is not None
    assert await store.claim("acme", _uuid(1), now=NOW + CLAIM_LEASE_MS - 1) is None

    later = NOW + CLAIM_LEASE_MS
    (retaken,) = await store.claim_queued(limit=5, now=later)
    assert (retaken["id"], retaken["started_at"]) == (_uuid(1), later)

    done = {"status": "succeeded", "baseline_score": 40, "with_skill_score": 70, "uplift": 30}
    assert not await store.finish("acme", _uuid(1), started_at=NOW, fields=done)
    results = [{"name": "s", "baseline_score": 40, "with_skill_score": 70, "uplift": 30}]
    assert await store.finish(
        "acme", _uuid(1), started_at=later, fields={**done, "results": results, "finished_at": later + 5}
    )
    row = await store.get("acme", _uuid(1))
    assert row is not None
    assert (row["status"], row["uplift"], row["results"], row["finished_at"]) == (
        "succeeded",
        30,
        results,
        later + 5,
    )


@parametrized
async def test_latest_succeeded_and_listings(store_settings: Any) -> None:
    store = get_skill_eval_store(store_settings)
    for n, uplift in ((1, 5), (2, 9)):
        await store.insert("acme", _eval(n, at=n))
        await store.claim("acme", _uuid(n), now=NOW + n)
        await store.finish(
            "acme",
            _uuid(n),
            started_at=NOW + n,
            fields={"status": "succeeded", "uplift": uplift, "finished_at": NOW + 10 * n},
        )
    await store.insert("acme", _eval(3, at=3))
    await store.claim("acme", _uuid(3), now=NOW)
    await store.finish("acme", _uuid(3), started_at=NOW, fields={"status": "failed", "finished_at": NOW + 99})

    latest = await store.latest_succeeded("acme", "invoice-triage", "0.1.0")
    assert latest is not None and (latest["id"], latest["uplift"]) == (_uuid(2), 9)
    assert await store.latest_succeeded("acme", "invoice-triage", "9.9.9") is None
    assert await store.latest_succeeded("globex", "invoice-triage", "0.1.0") is None

    listed = await store.list_for_skill("acme", "invoice-triage", limit=2)
    assert [r["id"] for r in listed] == [_uuid(3), _uuid(2)]
    rest = await store.list_for_skill("acme", "invoice-triage", before=(2, _uuid(2)))
    assert [r["id"] for r in rest] == [_uuid(1)]
    assert await store.list_for_skill("acme", "invoice-triage", version="0.2.0") == []


# -- policy ----------------------------------------------------------------------------------


@parametrized
async def test_the_policy_row_is_replaced_whole_and_tenant_scoped(store_settings: Any) -> None:
    store = get_skill_policy_store(store_settings)
    assert await store.get("acme") is None
    row = {
        "min_quality": 60,
        "block_on_advisory": True,
        "require_eval": True,
        "min_eval_uplift": 5,
        "updated_at": 1,
        "updated_by": "ops",
    }
    await store.put("acme", row)
    stored = await store.put("acme", {**row, "min_eval_uplift": None, "updated_at": 2})

    assert stored["min_eval_uplift"] is None and stored["updated_at"] == 2
    got = await store.get("acme")
    assert got is not None and {k: got[k] for k in row} == {**row, "min_eval_uplift": None, "updated_at": 2}
    assert await store.get("globex") is None
