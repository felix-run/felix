"""Consolidation's store surface, asserted against every backend.

`merge_duplicates` is a retirement route, and every retirement route in this store has at some
point been guarded on one arm and not the other. The unit tests run the twin; these run the
same assertions against Postgres when `FELIX_CONFORMANCE_DATABASE_URL` is set — the batch the
model is shown, the refusal of operator rows, the turn ordinal, the retirer stamp, and that a
pass is all-or-nothing per group.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.memory import store as memory_store

BACKENDS = ["memory", "postgres"]
parametrized = pytest.mark.parametrize("memory_settings", BACKENDS, indirect=True)

TENANT = "consolidation"
MANIFEST = "m"
AGENT = {"source": "assistant"}
OPERATOR = {"source": "management_api"}


async def _put(
    settings: Any,
    content: str,
    *,
    seq: int = 1,
    kind: str = "fact",
    topic: str | None = None,
    metadata: dict[str, str] | None = None,
    manifest: str = MANIFEST,
) -> str:
    row = await memory_store.put_memory(
        settings,
        TENANT,
        content=content,
        kind=kind,
        manifest_id=manifest,
        origin_seq=seq,
        topic_key=topic,
        metadata=dict(metadata or AGENT),
    )
    return str(row["id"])


async def _status(settings: Any, mem_id: str) -> str:
    return str((await memory_store.get_many(settings, TENANT, [mem_id]))[mem_id]["status"])


@parametrized
@pytest.mark.asyncio
async def test_the_batch_is_agent_rows_of_this_pool_counted_before_the_limit(
    memory_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One instant for every row, so the order below is decided by the id tie-break on both arms.
    # Unpinned, each write took the wall clock: the memory arm wrote inside one millisecond and the
    # Postgres arm did not, so the two orderings agreed on rule and differed on input.
    monkeypatch.setattr(memory_store, "now_ms", lambda: 1_900_000_000_000)
    agent = [await _put(memory_settings, f"Agent fact {i}.") for i in range(4)]
    await _put(memory_settings, "Operator fact.", metadata=OPERATOR)
    await _put(memory_settings, "Another manifest's fact.", manifest="other")
    gone = await _put(memory_settings, "Forgotten agent fact.")
    await memory_store.forget(memory_settings, TENANT, gone, source="assistant")

    count, rows = await memory_store.consolidation_batch(
        memory_settings, TENANT, manifest_id=MANIFEST, limit=3
    )

    assert count == 4
    assert len(rows) == 3 and {r["id"] for r in rows} <= set(agent)
    assert [r["id"] for r in rows] == sorted(agent, reverse=True)[:3]


@parametrized
@pytest.mark.asyncio
async def test_a_merge_supersedes_at_the_duplicates_own_turn_and_stamps_the_retirer(
    memory_settings: Any,
) -> None:
    keep = await _put(memory_settings, "The user prefers dark mode.", seq=2)
    dup = await _put(memory_settings, "User likes the dark theme.", seq=5)

    result = await memory_store.merge_duplicates(
        memory_settings, TENANT, manifest_id=MANIFEST, groups=[[dup, keep]]
    )

    assert result == (1, 0)
    rows = await memory_store.get_many(memory_settings, TENANT, [keep, dup])
    assert rows[keep]["status"] == memory_store.ACTIVE
    assert rows[dup]["status"] == memory_store.SUPERSEDED
    assert rows[dup]["superseded_by"] == keep
    assert rows[dup]["superseded_seq"] == 5
    assert rows[dup]["metadata"]["retired_by"] == memory_store.CONSOLIDATION_SOURCE
    assert rows[dup]["metadata"]["source"] == "assistant", "the writer's provenance was overwritten"
    # The interval closes at turn 5, in turn time: a clock value here would keep the
    # duplicate "current" in every as-of view for the next few billion turns.
    assert {r["id"] for r in await memory_store.as_of(memory_settings, TENANT, 6, manifest_id=MANIFEST)} == {
        keep
    }


@parametrized
@pytest.mark.asyncio
@pytest.mark.parametrize("operator_as", ["keep", "duplicate"])
async def test_an_operator_row_is_refused_in_either_role(memory_settings: Any, operator_as: str) -> None:
    curated = await _put(memory_settings, "Deploys need two approvers.", metadata=OPERATOR)
    agent = await _put(memory_settings, "Production deploys require two approvals.")
    group = [curated, agent] if operator_as == "keep" else [agent, curated]

    assert await memory_store.merge_duplicates(
        memory_settings, TENANT, manifest_id=MANIFEST, groups=[group]
    ) == (0, 1)
    assert await _status(memory_settings, curated) == memory_store.ACTIVE
    assert await _status(memory_settings, agent) == memory_store.ACTIVE


@parametrized
@pytest.mark.asyncio
async def test_a_bad_group_is_refused_whole_and_a_good_one_beside_it_applies(memory_settings: Any) -> None:
    keep = await _put(memory_settings, "The office is in Lisbon.", seq=1)
    dup = await _put(memory_settings, "The company office is in Lisbon.", seq=2)
    other_kind = await _put(memory_settings, "Answer in Lisbon time.", kind="instruction")
    a = await _put(memory_settings, "The user lives in Porto.", topic="user.city")
    b = await _put(memory_settings, "The user lives in Porto now.", topic="user.home")
    loose = await _put(memory_settings, "Home is Porto.")

    superseded, refused = await memory_store.merge_duplicates(
        memory_settings,
        TENANT,
        manifest_id=MANIFEST,
        groups=[[keep, dup, other_kind], [a, b], [loose, a], [dup, keep]],
    )

    # The last group is valid on its own; the first three are refused whole, so it applies.
    assert (superseded, refused) == (1, 3)
    assert await _status(memory_settings, dup) == memory_store.SUPERSEDED
    for mem_id in (keep, other_kind, a, b, loose):
        assert await _status(memory_settings, mem_id) == memory_store.ACTIVE


@parametrized
@pytest.mark.asyncio
async def test_the_store_keeps_the_oldest_member_whatever_order_it_is_given(memory_settings: Any) -> None:
    """Oldest by turn, an unknown turn counting as newest; then by clock, then by id."""
    unknown = await memory_store.put_memory(
        memory_settings, TENANT, content="Tea, not coffee.", manifest_id=MANIFEST, metadata=dict(AGENT)
    )
    newer = await _put(memory_settings, "The user drinks tea rather than coffee.", seq=9)
    oldest = await _put(memory_settings, "The user prefers tea to coffee.", seq=4)

    assert await memory_store.merge_duplicates(
        memory_settings, TENANT, manifest_id=MANIFEST, groups=[[unknown["id"], newer, oldest]]
    ) == (2, 0)
    rows = await memory_store.get_many(memory_settings, TENANT, [unknown["id"], newer, oldest])
    assert rows[oldest]["status"] == memory_store.ACTIVE
    assert rows[newer]["superseded_by"] == oldest
    assert rows[unknown["id"]]["superseded_by"] == oldest


@parametrized
@pytest.mark.asyncio
async def test_pools_are_listed_across_tenants(memory_settings: Any) -> None:
    from felix.db.session import rls_bypass

    await _put(memory_settings, "A fact.")
    await _put(memory_settings, "B fact.", manifest="other")
    with rls_bypass():
        pools = await memory_store.list_memory_pools(memory_settings)
    assert (TENANT, MANIFEST) in pools and (TENANT, "other") in pools
    assert pools == sorted(pools)
