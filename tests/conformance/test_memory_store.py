"""One contract, run against every long-term-memory backend.

The store's supersession logic exists twice — a dict walk for `memory://` and an
`INSERT … ON CONFLICT` plus `UPDATE` for Postgres — which is exactly the shape that
this audit has repeatedly found drifts when nothing compares the copies. The Postgres
half is also the half CI could not previously reach at all, because the schema it
needs is created by a migration and the suite used to build tables from the ORM.

Add a backend to `BACKENDS` and it inherits every assertion here.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.memory import store as memory_store
from felix.memory.store import ACTIVE, FORGOTTEN, SUPERSEDED

BACKENDS = ["memory", "postgres"]
parametrized = pytest.mark.parametrize("memory_settings", BACKENDS, indirect=True)

TENANT = "conformance"
MANIFEST = "m"


async def _put(settings: Any, content: str, **kw: Any) -> dict[str, Any]:
    return await memory_store.put_memory(settings, TENANT, content=content, manifest_id=MANIFEST, **kw)


@parametrized
@pytest.mark.asyncio
async def test_same_content_collapses_to_one_row(memory_settings: Any) -> None:
    a = await _put(memory_settings, "The user prefers dark mode.")
    b = await _put(memory_settings, "the   user prefers DARK mode.")
    assert a["id"] == b["id"]
    active = await memory_store.list_active(memory_settings, TENANT, manifest_id=MANIFEST)
    assert len(active) == 1


@parametrized
@pytest.mark.asyncio
async def test_topic_key_supersedes_the_previous_value(memory_settings: Any) -> None:
    old = await _put(memory_settings, "Timezone is UTC.", topic_key="user.timezone", origin_seq=4)
    new = await _put(memory_settings, "Timezone is CET.", topic_key="user.timezone", origin_seq=7)

    rows = await memory_store.get_many(memory_settings, TENANT, [old["id"], new["id"]])
    assert rows[old["id"]]["status"] == SUPERSEDED
    assert rows[old["id"]]["superseded_by"] == new["id"]
    assert rows[old["id"]]["superseded_seq"] == 7
    assert rows[new["id"]]["status"] == ACTIVE

    active = await memory_store.list_active(memory_settings, TENANT, manifest_id=MANIFEST)
    assert [r["content"] for r in active] == ["Timezone is CET."]


@parametrized
@pytest.mark.asyncio
async def test_supersession_is_scoped_to_one_manifest(memory_settings: Any) -> None:
    """Two agents sharing a topic key must not overwrite each other's memory."""
    mine = await memory_store.put_memory(
        memory_settings, TENANT, content="Mine.", manifest_id="agent-a", topic_key="k", origin_seq=1
    )
    await memory_store.put_memory(
        memory_settings, TENANT, content="Theirs.", manifest_id="agent-b", topic_key="k", origin_seq=2
    )
    rows = await memory_store.get_many(memory_settings, TENANT, [mine["id"]])
    assert rows[mine["id"]]["status"] == ACTIVE


@parametrized
@pytest.mark.asyncio
async def test_as_of_reconstructs_the_earlier_belief(memory_settings: Any) -> None:
    await _put(memory_settings, "Timezone is UTC.", topic_key="user.timezone", origin_seq=4)
    await _put(memory_settings, "Timezone is CET.", topic_key="user.timezone", origin_seq=7)

    at5 = await memory_store.as_of(memory_settings, TENANT, 5, manifest_id=MANIFEST)
    at9 = await memory_store.as_of(memory_settings, TENANT, 9, manifest_id=MANIFEST)
    assert [r["content"] for r in at5] == ["Timezone is UTC."]
    assert [r["content"] for r in at9] == ["Timezone is CET."]


@parametrized
@pytest.mark.asyncio
async def test_re_remembering_keeps_the_original_provenance(memory_settings: Any) -> None:
    first = await _put(memory_settings, "Stable fact.", origin_seq=2)
    await memory_store.forget(memory_settings, TENANT, first["id"])
    again = await _put(memory_settings, "Stable fact.", origin_seq=9)

    rows = await memory_store.get_many(memory_settings, TENANT, [again["id"]])
    assert rows[again["id"]]["status"] == ACTIVE
    assert rows[again["id"]]["origin_seq"] == 2


@parametrized
@pytest.mark.asyncio
async def test_forget_hides_without_deleting(memory_settings: Any) -> None:
    row = await _put(memory_settings, "Regrettable fact.", origin_seq=3)
    assert await memory_store.forget(memory_settings, TENANT, row["id"]) is True
    assert await memory_store.list_active(memory_settings, TENANT, manifest_id=MANIFEST) == []

    rows = await memory_store.get_many(memory_settings, TENANT, [row["id"]])
    assert rows[row["id"]]["status"] == FORGOTTEN
    assert rows[row["id"]]["superseded_seq"] is None


@parametrized
@pytest.mark.asyncio
async def test_forget_reports_whether_it_found_anything(memory_settings: Any) -> None:
    assert await memory_store.forget(memory_settings, TENANT, "no-such-id") is False


@parametrized
@pytest.mark.asyncio
async def test_get_many_resolves_in_one_call(memory_settings: Any) -> None:
    ids = [(await _put(memory_settings, f"fact {i}"))["id"] for i in range(3)]
    rows = await memory_store.get_many(memory_settings, TENANT, [*ids, "missing"])
    assert set(rows) == set(ids)
    assert await memory_store.get_many(memory_settings, TENANT, []) == {}


@parametrized
@pytest.mark.asyncio
async def test_kind_and_manifest_filters(memory_settings: Any) -> None:
    await _put(memory_settings, "a fact", kind="fact")
    await _put(memory_settings, "a procedure", kind="procedure")
    await memory_store.put_memory(memory_settings, TENANT, content="elsewhere", manifest_id="other")

    facts = await memory_store.list_active(memory_settings, TENANT, manifest_id=MANIFEST, kind="fact")
    assert [r["content"] for r in facts] == ["a fact"]
    mine = await memory_store.list_active(memory_settings, TENANT, manifest_id=MANIFEST)
    assert len(mine) == 2


@parametrized
@pytest.mark.asyncio
async def test_current_turn_seq_tracks_the_highest_ordinal(memory_settings: Any) -> None:
    assert await memory_store.current_turn_seq(memory_settings, TENANT, manifest_id=MANIFEST) == 0
    await _put(memory_settings, "a", origin_seq=3)
    await _put(memory_settings, "b", origin_seq=11)
    assert await memory_store.current_turn_seq(memory_settings, TENANT, manifest_id=MANIFEST) == 11


@parametrized
@pytest.mark.asyncio
async def test_importance_is_clamped(memory_settings: Any) -> None:
    """It multiplies a ranking score, so an out-of-range value would distort recall."""
    high = await _put(memory_settings, "very important", importance=9.0)
    low = await _put(memory_settings, "very unimportant", importance=-3.0)
    rows = await memory_store.get_many(memory_settings, TENANT, [high["id"], low["id"]])
    assert rows[high["id"]]["importance"] == 1.0
    assert rows[low["id"]]["importance"] == 0.0


@parametrized
@pytest.mark.asyncio
async def test_a_memory_without_an_embedding_can_be_stored(memory_settings: Any) -> None:
    """The regression that made this table unusable on Postgres.

    `memory_vectors.embedding` has been `vector(768) NOT NULL` with no default since
    0001_baseline, and `put_memory` never supplied a vector — so every insert raised
    NotNullViolation, and the only caller swallowed it into a debug log. Storing
    without an embedder configured is the default path, not an edge case.
    """
    row = await _put(memory_settings, "A fact with no vector attached.")
    stored = await memory_store.get_many(memory_settings, TENANT, [row["id"]])
    assert stored[row["id"]]["content"] == "A fact with no vector attached."
    assert stored[row["id"]]["embedding_dim"] is None


# --- hybrid recall ----------------------------------------------------------------
#
# The channels are three SQL statements on Postgres and three Python loops in the
# twin. Nothing but this compares them, and the SQL cannot run at all without the
# generated columns migration 0009 creates.


class _AxisEmbedder:
    """Deterministic embeddings — no model, no network.

    Sized to the real column (`vector(768)`, from 0001_baseline) with the signal in
    the first two components and zeros elsewhere. A shorter vector is rejected, which
    is the correct behaviour and not what this test is about.
    """

    enabled = True
    dim = 768

    async def embed(self, texts: Any) -> list[list[float]]:
        out = []
        for text in texts:
            low = str(text).lower()
            head = [1.0, 0.0] if ("automobile" in low or "car" in low) else [0.0, 1.0]
            out.append(head + [0.0] * (self.dim - 2))
        return out


@parametrized
@pytest.mark.asyncio
async def test_recall_finds_by_content_not_recency(memory_settings: Any) -> None:
    from felix.memory.recall import recall

    await _put(memory_settings, "The deployment runbook lives in the ops repository.")
    await _put(memory_settings, "Lunch was pleasant.")

    hits = await recall(memory_settings, TENANT, "where is the runbook", manifest_id=MANIFEST)
    assert hits
    assert "runbook" in hits[0].content


@parametrized
@pytest.mark.asyncio
async def test_topic_channel_finds_a_dotted_identifier(memory_settings: Any) -> None:
    from felix.memory.recall import recall

    await _put(memory_settings, "CET.", topic_key="user.timezone")
    hits = await recall(memory_settings, TENANT, "what timezone", manifest_id=MANIFEST)
    assert hits
    assert hits[0].topic_key == "user.timezone"
    assert "topic" in hits[0].channels


@parametrized
@pytest.mark.asyncio
async def test_vector_channel_finds_a_paraphrase(memory_settings: Any) -> None:
    """No token overlap at all, so only the vector channel can find this."""
    from felix.memory.recall import recall

    emb = _AxisEmbedder()
    vectors = await emb.embed(["The automobile is red.", "The soup is cold."])
    await _put(memory_settings, "The automobile is red.", embedding=vectors[0])
    await _put(memory_settings, "The soup is cold.", embedding=vectors[1])

    hits = await recall(memory_settings, TENANT, "my car", manifest_id=MANIFEST, embedder=emb)
    assert hits
    assert "automobile" in hits[0].content
    assert "vector" in hits[0].channels


@parametrized
@pytest.mark.asyncio
async def test_superseded_and_forgotten_are_not_recalled(memory_settings: Any) -> None:
    from felix.memory.recall import recall

    await _put(memory_settings, "Timezone is UTC.", topic_key="user.timezone", origin_seq=1)
    await _put(memory_settings, "Timezone is CET.", topic_key="user.timezone", origin_seq=2)
    gone = await _put(memory_settings, "Timezone trivia nobody wants.")
    await memory_store.forget(memory_settings, TENANT, gone["id"])

    hits = await recall(memory_settings, TENANT, "timezone", manifest_id=MANIFEST)
    assert [h.content for h in hits] == ["Timezone is CET."]


@parametrized
@pytest.mark.asyncio
async def test_recall_survives_a_query_full_of_punctuation(memory_settings: Any) -> None:
    """User text goes straight into the query; it must not break the SQL."""
    from felix.memory.recall import recall

    await _put(memory_settings, "The runbook is in the ops repository.")
    for hostile in ("runbook & | ! ( ) :*", "'; DROP TABLE memory_vectors; --", "???"):
        await recall(memory_settings, TENANT, hostile, manifest_id=MANIFEST)

    assert await memory_store.list_active(memory_settings, TENANT, manifest_id=MANIFEST)


@parametrized
@pytest.mark.asyncio
async def test_a_stored_vector_round_trips(memory_settings: Any) -> None:
    """`embedding_dim` records what was actually stored, on both backends."""
    emb = _AxisEmbedder()
    vector = (await emb.embed(["The automobile is red."]))[0]
    row = await _put(memory_settings, "The automobile is red.", embedding=vector)

    stored = await memory_store.get_many(memory_settings, TENANT, [row["id"]])
    assert stored[row["id"]]["embedding_dim"] == emb.dim
