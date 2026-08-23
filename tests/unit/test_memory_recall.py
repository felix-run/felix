"""Hybrid recall: fusion arithmetic, channel behaviour, and graceful degradation.

The fusion function is pure, so it is tested directly rather than through a database.
The channels are tested on the `memory://` twin, which implements all three for real
— a channel faked there is a channel nothing tests, since that is the path CI runs.
"""

from __future__ import annotations

import pytest
from felix.config import Settings
from felix.memory import store as memory_store
from felix.memory.embedder import NullEmbedder, build_embedder
from felix.memory.recall import RRF_K, recall, rrf_fuse

TENANT = "t-recall"
MANIFEST = "m"


@pytest.fixture(autouse=True)
def _clean() -> None:
    memory_store._memory_rows.clear()


def _settings(**kw: object) -> Settings:
    return Settings(database_url="memory://recall", **kw)


async def _put(content: str, **kw: object):
    return await memory_store.put_memory(_settings(), TENANT, content=content, manifest_id=MANIFEST, **kw)


# --- fusion ---------------------------------------------------------------------


def test_rrf_rewards_agreement_across_channels() -> None:
    """The property worth having: two weak votes beat one strong one."""
    fused = rrf_fuse({"fts": ["b", "a"], "vector": ["b", "c"]})
    assert fused["b"] > fused["a"]
    assert fused["b"] > fused["c"]


def test_rrf_ignores_score_magnitude_entirely() -> None:
    """Only rank position matters — that is what makes the channels comparable."""
    assert rrf_fuse({"fts": ["x"]})["x"] == pytest.approx(1.0 / (RRF_K + 1))


def test_rrf_of_nothing_is_nothing() -> None:
    assert rrf_fuse({}) == {}
    assert rrf_fuse({"fts": []}) == {}


def test_rrf_position_decays() -> None:
    fused = rrf_fuse({"fts": ["first", "second", "third"]})
    assert fused["first"] > fused["second"] > fused["third"]


# --- channels -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recall_finds_by_content_not_recency() -> None:
    """The regression: recall used to be `ORDER BY created_at`."""
    await _put("The deployment runbook lives in the ops repository.")
    await _put("Lunch was pleasant.")  # newer, irrelevant

    hits = await recall(_settings(), TENANT, "where is the runbook", manifest_id=MANIFEST)
    assert hits
    assert "runbook" in hits[0].content


@pytest.mark.asyncio
async def test_topic_key_channel_finds_a_dotted_identifier() -> None:
    """ "what timezone" has no literal overlap with the content, only the topic key."""
    await _put("CET.", topic_key="user.timezone")
    hits = await recall(_settings(), TENANT, "what timezone", manifest_id=MANIFEST)
    assert hits
    assert hits[0].topic_key == "user.timezone"
    assert "topic" in hits[0].channels


@pytest.mark.asyncio
async def test_superseded_memories_are_not_recalled() -> None:
    await _put("Timezone is UTC.", topic_key="user.timezone", origin_seq=1)
    await _put("Timezone is CET.", topic_key="user.timezone", origin_seq=2)

    hits = await recall(_settings(), TENANT, "timezone", manifest_id=MANIFEST)
    assert [h.content for h in hits] == ["Timezone is CET."]


@pytest.mark.asyncio
async def test_forgotten_memories_are_not_recalled() -> None:
    row = await _put("Please forget the staging password hint.")
    await memory_store.forget(_settings(), TENANT, row["id"])
    assert await recall(_settings(), TENANT, "staging password", manifest_id=MANIFEST) == []


@pytest.mark.asyncio
async def test_recall_is_scoped_to_the_manifest() -> None:
    await memory_store.put_memory(
        _settings(), TENANT, content="Agent A knows the runbook.", manifest_id="agent-a"
    )
    hits = await recall(_settings(), TENANT, "runbook", manifest_id="agent-b")
    assert hits == []


@pytest.mark.asyncio
async def test_kind_filter() -> None:
    await _put("A stable fact about the runbook.", kind="fact")
    await _put("A procedure about the runbook.", kind="procedure")

    hits = await recall(_settings(), TENANT, "runbook", manifest_id=MANIFEST, kinds=["procedure"])
    assert [h.kind for h in hits] == ["procedure"]


@pytest.mark.asyncio
async def test_empty_query_returns_nothing() -> None:
    await _put("Something.")
    assert await recall(_settings(), TENANT, "   ", manifest_id=MANIFEST) == []


@pytest.mark.asyncio
async def test_limit_is_respected() -> None:
    for i in range(10):
        await _put(f"Runbook note number {i}.")
    hits = await recall(_settings(), TENANT, "runbook note", manifest_id=MANIFEST, limit=3)
    assert len(hits) == 3


@pytest.mark.asyncio
async def test_hits_report_which_channels_found_them() -> None:
    await _put("CET is the timezone.", topic_key="user.timezone")
    hits = await recall(_settings(), TENANT, "timezone", manifest_id=MANIFEST)
    assert set(hits[0].channels) >= {"fts", "topic"}


# --- weighting ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kind_weighting_breaks_a_tie_toward_facts() -> None:
    await _put("The runbook matters.", kind="event")
    await _put("The runbook matters too.", kind="fact")
    hits = await recall(_settings(), TENANT, "runbook matters", manifest_id=MANIFEST)
    assert hits[0].kind == "fact"


@pytest.mark.asyncio
async def test_importance_lifts_an_otherwise_equal_memory() -> None:
    await _put("The runbook is here.", importance=0.1)
    await _put("The runbook is there.", importance=1.0)
    hits = await recall(_settings(), TENANT, "runbook", manifest_id=MANIFEST)
    assert hits[0].importance == 1.0


# --- degradation ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_recall_works_with_no_embedder_at_all() -> None:
    """The lean install: no extras, no key, no model — and recall still returns."""
    embedder = build_embedder(_settings())
    assert isinstance(embedder, NullEmbedder)
    assert embedder.enabled is False

    await _put("The deployment runbook lives in the ops repository.")
    hits = await recall(_settings(), TENANT, "runbook", manifest_id=MANIFEST, embedder=embedder)
    assert hits
    assert "vector" not in hits[0].channels


@pytest.mark.asyncio
async def test_vector_channel_finds_a_paraphrase_full_text_cannot() -> None:
    """The reason the vector channel exists: no shared tokens at all."""

    class _Embedder:
        enabled = True
        dim = 2

        async def embed(self, texts):
            # "car"-ish and "auto"-ish land in the same direction; "soup" does not.
            out = []
            for t in texts:
                low = t.lower()
                if "automobile" in low or "car" in low:
                    out.append([1.0, 0.0])
                else:
                    out.append([0.0, 1.0])
            return out

    emb = _Embedder()
    vectors = await emb.embed(["The automobile is red.", "The soup is cold."])
    await _put("The automobile is red.", embedding=vectors[0])
    await _put("The soup is cold.", embedding=vectors[1])

    hits = await recall(_settings(), TENANT, "my car", manifest_id=MANIFEST, embedder=emb)
    assert hits
    assert "automobile" in hits[0].content
    assert "vector" in hits[0].channels


@pytest.mark.asyncio
async def test_a_failing_embedder_loses_a_channel_not_the_turn() -> None:
    class _Broken:
        enabled = True
        dim = 2

        async def embed(self, texts):
            raise RuntimeError("embedding service is down")

    await _put("The deployment runbook lives in the ops repository.")
    hits = await recall(_settings(), TENANT, "runbook", manifest_id=MANIFEST, embedder=_Broken())
    assert hits, "a dead embedding endpoint must not take out full-text recall"
    assert "vector" not in hits[0].channels
