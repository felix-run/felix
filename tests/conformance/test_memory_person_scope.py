"""Person pools in the long-term memory store, against every backend.

A row belongs to one pool (`memory.store.MemoryPool`): an agent's own, `(tenant, manifest,
principal='')`, or a person's, `(tenant, principal)`. Inside a tenant nothing but the store's own
filter keeps one person's facts from another -- RLS isolates tenants only -- and that filter is
written three times (a dict walk, SQLAlchemy, and recall's `text()` fragments), so each rule here
runs on both arms.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.memory import store as memory_store
from felix.memory.assign import ASSIGN_SOURCE, assign_to_person
from felix.memory.recall import recall
from felix.memory.store import ACTIVE, FORGOTTEN, SUPERSEDED, MemoryFilter, MemoryNotStored, MemoryPool

BACKENDS = ["memory", "postgres"]
parametrized = pytest.mark.parametrize("memory_settings", BACKENDS, indirect=True)

TENANT = "conformance"
MANIFEST = "assistant"
ALICE = "felix-self|github:1"
BOB = "felix-self|github:2"
OPERATOR = {"source": "management_api"}


async def _put(
    settings: Any, content: str, *, principal: str = "", manifest: str = MANIFEST, **kw: Any
) -> dict[str, Any]:
    pool = MemoryPool.person(manifest, principal) if principal else MemoryPool.agent(manifest)
    return await memory_store.put_memory(settings, TENANT, pool=pool, content=content, **kw)


async def _row(settings: Any, mem_id: str) -> dict[str, Any]:
    return (await memory_store.get_many(settings, TENANT, [mem_id]))[mem_id]


def _contents(rows: list[dict[str, Any]]) -> set[str]:
    return {str(r["content"]) for r in rows}


@parametrized
async def test_a_person_sees_their_pool_and_the_agents_curated_rows_only(memory_settings: Any) -> None:
    await _put(memory_settings, "Alice lives in Lisbon.", principal=ALICE)
    await _put(memory_settings, "Bob lives in Oslo.", principal=BOB)
    await _put(memory_settings, "The office closes at six.", metadata=OPERATOR)
    await _put(memory_settings, "An agent-written note in the agent pool.")
    await _put(memory_settings, "Another agent's curated note.", manifest="cowork", metadata=OPERATOR)

    def read(pool: MemoryPool) -> Any:
        return memory_store.list_active(memory_settings, TENANT, scope=pool)

    assert _contents(await read(MemoryPool.person(MANIFEST, ALICE))) == {
        "Alice lives in Lisbon.",
        "The office closes at six.",
    }
    assert _contents(await read(MemoryPool.person(MANIFEST, BOB))) == {
        "Bob lives in Oslo.",
        "The office closes at six.",
    }
    # Person scope with no person: the curated rows and nothing else.
    assert _contents(await read(MemoryPool.personless(MANIFEST))) == {"The office closes at six."}
    # `scope: agent`: the agent's whole pool, as before people had memory, and no person's.
    assert _contents(await read(MemoryPool.agent(MANIFEST))) == {
        "The office closes at six.",
        "An agent-written note in the agent pool.",
    }


@parametrized
async def test_recall_keeps_to_the_pool_on_every_channel(memory_settings: Any) -> None:
    await _put(
        memory_settings, "Alice's timezone is Lisbon time.", principal=ALICE, topic_key="user.timezone"
    )
    await _put(memory_settings, "Bob's timezone is Oslo time.", principal=BOB, topic_key="user.timezone")
    await _put(
        memory_settings, "Office timezone is UTC time.", topic_key="office.timezone", metadata=OPERATOR
    )
    await _put(memory_settings, "Agent heard a timezone once.", topic_key="agent.timezone")
    await _put(memory_settings, "Cowork timezone is CET time.", manifest="cowork", metadata=OPERATOR)

    async def hits(scope: Any) -> set[str]:
        return {h.content for h in await recall(memory_settings, TENANT, "what timezone", scope=scope)}

    assert await hits(MemoryPool.person(MANIFEST, BOB)) == {
        "Bob's timezone is Oslo time.",
        "Office timezone is UTC time.",
    }
    assert await hits(MemoryPool.personless(MANIFEST)) == {"Office timezone is UTC time."}
    assert await hits(MemoryPool.agent(MANIFEST)) == {
        "Office timezone is UTC time.",
        "Agent heard a timezone once.",
    }
    # The operator's filter: one person's pool, or every pool when it names none.
    assert await hits(MemoryFilter(principal=ALICE)) == {"Alice's timezone is Lisbon time."}
    assert len(await hits(MemoryFilter())) == 5


@parametrized
async def test_two_agents_storing_one_fact_about_a_person_share_one_row(memory_settings: Any) -> None:
    first = await _put(memory_settings, "Alice's birthday is May 4.", principal=ALICE, manifest="assistant")
    second = await _put(memory_settings, "alice's  birthday is May 4.", principal=ALICE, manifest="cowork")
    assert first["id"] == second["id"]
    assert first["id"].startswith(memory_store.PERSON_ID_PREFIX)
    # Credited to the agent that wrote it first, on both arms.
    assert (await _row(memory_settings, first["id"]))["manifest_id"] == "assistant"
    assert (
        len(await memory_store.list_active(memory_settings, TENANT, scope=MemoryFilter(principal=ALICE))) == 1
    )


@parametrized
async def test_an_agent_pool_id_is_unchanged_and_never_a_person_id(memory_settings: Any) -> None:
    row = await _put(memory_settings, "A fact.")
    assert row["id"] == memory_store.memory_id(MANIFEST, "A fact.")
    assert row["id"] != memory_store.person_memory_id(ALICE, "A fact.")
    # Same sentence, two people: two rows.
    assert memory_store.person_memory_id(ALICE, "A fact.") != memory_store.person_memory_id(BOB, "A fact.")


@parametrized
async def test_a_personless_pool_takes_no_writes(memory_settings: Any) -> None:
    with pytest.raises(MemoryNotStored) as refused:
        await memory_store.put_memory(
            memory_settings, TENANT, pool=MemoryPool.personless(MANIFEST), content="For no one."
        )
    assert refused.value.reason == "read_only"
    assert await memory_store.list_active(memory_settings, TENANT, scope=MemoryFilter()) == []


@parametrized
async def test_supersession_by_topic_stays_inside_one_pool(memory_settings: Any) -> None:
    bobs = await _put(memory_settings, "Bob: UTC.", principal=BOB, topic_key="user.timezone")
    agents = await _put(memory_settings, "Agent: UTC.", topic_key="user.timezone")
    alices = await _put(memory_settings, "Alice: UTC.", principal=ALICE, topic_key="user.timezone")
    # An operator-ranked write is the one that retires by topic, and only in its own pool: into the
    # agent pool it retires the agent's row and leaves both people's.
    curated = await _put(memory_settings, "Agent: CET.", topic_key="user.timezone", metadata=OPERATOR)
    assert (await _row(memory_settings, agents["id"]))["superseded_by"] == curated["id"]
    assert (await _row(memory_settings, alices["id"]))["status"] == ACTIVE
    assert (await _row(memory_settings, bobs["id"]))["status"] == ACTIVE
    # Into Alice's, it retires hers and leaves Bob's.
    newer = await _put(
        memory_settings, "Alice: CET.", principal=ALICE, topic_key="user.timezone", metadata=OPERATOR
    )
    assert (await _row(memory_settings, alices["id"]))["status"] == SUPERSEDED
    assert (await _row(memory_settings, alices["id"]))["superseded_by"] == newer["id"]
    assert (await _row(memory_settings, bobs["id"]))["status"] == ACTIVE
    assert (await _row(memory_settings, curated["id"]))["status"] == ACTIVE


@parametrized
async def test_forget_through_a_pool_reaches_only_that_pool(memory_settings: Any) -> None:
    bobs = await _put(memory_settings, "Bob's secret.", principal=BOB)
    # Agent-written, so the agent's rank would allow the forget: only the pool refuses it.
    shared = await _put(memory_settings, "Shared agent note.")
    alices = await _put(memory_settings, "Alice's note.", principal=ALICE)
    pool = MemoryPool.person(MANIFEST, ALICE)

    async def forget(mem_id: str, by: MemoryPool) -> bool:
        return await memory_store.forget(memory_settings, TENANT, mem_id, source="remember_tool", pool=by)

    assert await forget(bobs["id"], pool) is False
    assert await forget(shared["id"], pool) is False
    # A person-less pool forgets nothing, and an agent-scoped one cannot reach a person's row.
    assert await forget(alices["id"], MemoryPool.personless(MANIFEST)) is False
    assert await forget(shared["id"], MemoryPool.personless(MANIFEST)) is False
    assert await forget(alices["id"], MemoryPool.agent(MANIFEST)) is False
    assert (await _row(memory_settings, bobs["id"]))["status"] == ACTIVE
    assert (await _row(memory_settings, shared["id"]))["status"] == ACTIVE
    assert (await _row(memory_settings, alices["id"]))["status"] == ACTIVE
    assert await forget(alices["id"], pool) is True
    assert (await _row(memory_settings, alices["id"]))["status"] == FORGOTTEN


@parametrized
async def test_supersede_through_a_pool_reaches_only_that_pool(memory_settings: Any) -> None:
    bobs = await _put(memory_settings, "Bob's fact.", principal=BOB)
    await memory_store.supersede(
        memory_settings,
        TENANT,
        bobs["id"],
        3,
        source="management_api",
        pool=MemoryPool.person(MANIFEST, ALICE),
    )
    assert (await _row(memory_settings, bobs["id"]))["status"] == ACTIVE


@parametrized
async def test_as_of_and_forgotten_listing_filter_by_principal(memory_settings: Any) -> None:
    a = await _put(memory_settings, "Alice at turn one.", principal=ALICE, origin_seq=1)
    await _put(memory_settings, "Agent at turn one.", origin_seq=1)

    async def at(scope: MemoryFilter) -> set[str]:
        return _contents(await memory_store.as_of(memory_settings, TENANT, 5, scope=scope))

    assert await at(MemoryFilter(principal=ALICE)) == {"Alice at turn one."}
    assert await at(MemoryFilter(principal="")) == {"Agent at turn one."}
    assert len(await at(MemoryFilter())) == 2

    await memory_store.forget(memory_settings, TENANT, a["id"], source="management_api")
    forgotten = await memory_store.list_forgotten(
        memory_settings, TENANT, scope=MemoryFilter(principal=ALICE)
    )
    assert [r["id"] for r in forgotten] == [a["id"]]
    assert await memory_store.list_forgotten(memory_settings, TENANT, scope=MemoryFilter(principal=BOB)) == []


@parametrized
async def test_consolidation_pools_and_merges_never_cross_people(memory_settings: Any) -> None:
    a1 = await _put(memory_settings, "Alice likes tea.", principal=ALICE)
    a2 = await _put(memory_settings, "Alice enjoys tea.", principal=ALICE)
    b1 = await _put(memory_settings, "Bob likes tea.", principal=BOB)
    await _put(memory_settings, "Agent fact.")

    pools = await memory_store.list_memory_pools(memory_settings)
    assert {p for p in pools if p.tenant_id == TENANT} == {
        (TENANT, MANIFEST, ""),
        (TENANT, MANIFEST, ALICE),
        (TENANT, MANIFEST, BOB),
    }
    count, rows = await memory_store.consolidation_batch(
        memory_settings, TENANT, manifest_id=MANIFEST, principal=ALICE, limit=10
    )
    assert count == 2 and {r["id"] for r in rows} == {a1["id"], a2["id"]}

    async def merge(principal: str, group: list[str]) -> tuple[int, int]:
        return await memory_store.merge_duplicates(
            memory_settings, TENANT, manifest_id=MANIFEST, principal=principal, groups=[group]
        )

    # A group reaching into Bob's pool is refused whole, and the agent pool's merge cannot see Alice's.
    assert await merge(ALICE, [a1["id"], b1["id"]]) == (0, 1)
    assert await merge("", [a1["id"], a2["id"]]) == (0, 1)
    assert await merge(ALICE, [a1["id"], a2["id"]]) == (1, 0)
    assert (await _row(memory_settings, b1["id"]))["status"] == ACTIVE


@parametrized
async def test_task_expiry_and_the_backfill_reach_a_person_pool(memory_settings: Any) -> None:
    task = await _put(memory_settings, "Alice wants a report.", principal=ALICE, kind="task")
    assert await memory_store.expire_tasks(memory_settings, memory_store.now_ms() + 1) == 1
    assert (await _row(memory_settings, task["id"]))["status"] == FORGOTTEN

    fact = await _put(memory_settings, "Alice has a vector coming.", principal=ALICE)
    missing = await memory_store.missing_embeddings(memory_settings, limit=10)
    assert (TENANT, fact["id"], "Alice has a vector coming.") in missing
    vector = [1.0] + [0.0] * 767
    dim = await memory_store.configured_embedding_dim(memory_settings)
    written = await memory_store.write_embeddings(
        memory_settings, [(TENANT, fact["id"], "")], [vector], model="fake", expected_dim=dim
    )
    assert written == 1
    assert (TENANT, fact["id"], "Alice has a vector coming.") not in await memory_store.missing_embeddings(
        memory_settings, limit=10
    )


# --- `felix memory assign` ---------------------------------------------------------------


async def _seed_agent_pool(settings: Any) -> dict[str, dict[str, Any]]:
    old = await _put(settings, "Timezone is UTC.", topic_key="user.timezone", origin_seq=1)
    new = await _put(settings, "Timezone is CET.", topic_key="user.timezone", origin_seq=2, metadata=OPERATOR)
    gone = await _put(settings, "A forgotten fact.", origin_seq=1)
    await memory_store.forget(settings, TENANT, gone["id"], source="management_api")
    dup = await _put(settings, "Alice likes tea.", origin_seq=1)
    vec = await _put(settings, "Alice has a vector.", origin_seq=1, embedding=[1.0] + [0.0] * 767)
    return {"old": old, "new": new, "gone": gone, "dup": dup, "vec": vec}


def _moved(seeded: dict[str, dict[str, Any]], key: str) -> str:
    return memory_store.person_memory_id(ALICE, seeded[key]["content"])


@parametrized
async def test_assign_dry_run_counts_agent_rows_and_writes_nothing(memory_settings: Any) -> None:
    seeded = await _seed_agent_pool(memory_settings)
    await _put(memory_settings, "Alice likes tea.", principal=ALICE)

    report = await assign_to_person(memory_settings, TENANT, manifest_id=MANIFEST, person=ALICE, dry_run=True)
    # Four agent-written rows; the operator's stays where every person already reads it.
    assert (report.scanned, report.moved, report.merged) == (4, 3, 1)
    report = await assign_to_person(
        memory_settings, TENANT, manifest_id=MANIFEST, person=ALICE, dry_run=True, include_operator=True
    )
    assert (report.scanned, report.moved, report.merged) == (5, 4, 1)
    for row in seeded.values():
        assert (await _row(memory_settings, row["id"]))["principal"] == ""


@parametrized
async def test_assign_leaves_operator_rows_unless_asked(memory_settings: Any) -> None:
    seeded = await _seed_agent_pool(memory_settings)
    await assign_to_person(memory_settings, TENANT, manifest_id=MANIFEST, person=ALICE)
    curated = await _row(memory_settings, seeded["new"]["id"])
    assert (curated["principal"], curated["status"]) == ("", ACTIVE)
    # Moved, the agent-written row's pointer still names the operator row that retired it.
    assert (await _row(memory_settings, _moved(seeded, "old")))["superseded_by"] == seeded["new"]["id"]


@parametrized
async def test_assign_moves_the_pool_merges_what_the_person_holds_and_reruns_as_a_no_op(
    memory_settings: Any,
) -> None:
    seeded = await _seed_agent_pool(memory_settings)
    held = await _put(memory_settings, "Alice likes tea.", principal=ALICE, origin_seq=9)
    other = await _put(memory_settings, "Another agent's.", manifest="cowork")
    bobs = await _put(memory_settings, "Bob likes coffee.", principal=BOB)

    report = await assign_to_person(
        memory_settings, TENANT, manifest_id=MANIFEST, person=ALICE, include_operator=True
    )
    assert (report.scanned, report.moved, report.merged) == (5, 4, 1)

    moved = [_moved(seeded, k) for k in ("old", "new", "gone", "vec")]
    rows = await memory_store.get_many(memory_settings, TENANT, moved)
    assert all(r["principal"] == ALICE for r in rows.values()) and len(rows) == 4
    # Status, provenance and the supersession chain all travel, the pointer re-aimed at the new id.
    assert rows[_moved(seeded, "old")]["status"] == SUPERSEDED
    assert rows[_moved(seeded, "old")]["superseded_by"] == _moved(seeded, "new")
    assert rows[_moved(seeded, "new")]["status"] == ACTIVE
    assert rows[_moved(seeded, "new")]["metadata"]["source"] == "management_api"
    assert rows[_moved(seeded, "gone")]["status"] == FORGOTTEN
    assert rows[_moved(seeded, "old")]["origin_seq"] == 1
    # The vector came along: the backfill does not see the moved row as vectorless.
    assert all(
        i != _moved(seeded, "vec")
        for _, i, _ in await memory_store.missing_embeddings(memory_settings, limit=50)
    )
    assert await memory_store.list_active(memory_settings, TENANT, scope=MemoryPool.agent(MANIFEST)) == []

    # The duplicate: the person's row is untouched, the moved copy retired by it, in her pool.
    kept = await _row(memory_settings, held["id"])
    assert (kept["status"], kept["origin_seq"], kept["principal"]) == (ACTIVE, 9, ALICE)
    dup = await _row(memory_settings, seeded["dup"]["id"])
    assert (dup["status"], dup["superseded_by"], dup["principal"]) == (SUPERSEDED, held["id"], ALICE)
    assert dup["metadata"][memory_store.RETIRED_BY_KEY] == ASSIGN_SOURCE

    # Another agent's pool, and another person's, are not this one.
    assert (await _row(memory_settings, other["id"]))["principal"] == ""
    assert (await _row(memory_settings, bobs["id"]))["principal"] == BOB

    again = await assign_to_person(
        memory_settings, TENANT, manifest_id=MANIFEST, person=ALICE, include_operator=True
    )
    assert (again.scanned, again.moved, again.merged) == (0, 0, 0)


@parametrized
async def test_a_write_onto_another_pools_row_is_refused_before_it_retires_anything(
    memory_settings: Any,
) -> None:
    """The agent-pool id a merged duplicate kept now belongs to Alice's pool. A later agent-pool write
    of that sentence -- under an operator's topic, which would sweep the agent pool -- is refused whole."""
    seeded = await _seed_agent_pool(memory_settings)
    await _put(memory_settings, "Alice likes tea.", principal=ALICE)
    await assign_to_person(memory_settings, TENANT, manifest_id=MANIFEST, person=ALICE)
    standing = await _put(memory_settings, "Tea time is four.", topic_key="office.tea", metadata=OPERATOR)

    with pytest.raises(MemoryNotStored) as refused:
        await _put(memory_settings, "Alice likes tea.", topic_key="office.tea", metadata=OPERATOR)
    assert refused.value.reason == "other_pool"
    dup = await _row(memory_settings, seeded["dup"]["id"])
    assert (dup["status"], dup["principal"]) == (SUPERSEDED, ALICE)
    assert (await _row(memory_settings, standing["id"]))["status"] == ACTIVE, (
        "the refused write swept its topic"
    )


@parametrized
async def test_assign_re_aims_a_pointer_across_batches(memory_settings: Any) -> None:
    older = await _put(memory_settings, "Older value.", origin_seq=1)
    newer = await _put(memory_settings, "Newer value.", origin_seq=2)
    await memory_store.supersede(
        memory_settings, TENANT, older["id"], 2, superseded_by=newer["id"], source="management_api"
    )
    bobs = await _put(memory_settings, "Bob's own.", principal=BOB)
    for i in range(3):
        await _put(memory_settings, f"Fact number {i}.")

    report = await assign_to_person(memory_settings, TENANT, manifest_id=MANIFEST, person=ALICE, batch_size=1)
    assert (report.scanned, report.moved, report.merged) == (5, 5, 0)
    moved_older = await _row(memory_settings, memory_store.person_memory_id(ALICE, "Older value."))
    assert moved_older["superseded_by"] == memory_store.person_memory_id(ALICE, "Newer value.")
    assert (
        len(await memory_store.list_active(memory_settings, TENANT, scope=MemoryFilter(principal=ALICE))) == 4
    )
    assert (await _row(memory_settings, bobs["id"]))["principal"] == BOB
