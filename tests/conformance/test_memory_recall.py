"""One contract for `recall()`, run against the in-memory twin and Postgres.

`recall` is the path a fact reaches a prompt by that `list_active` is not: the `recall` tool
and `GET /memory/recall`. It is also the memory read with the most to hold together — three
channels on each backend, fused by reciprocal rank — and it had no conformance arm at all,
which is how the ordering survey that fixed `list_active` missed it. Those channels are
hand-rolled `sorted(...)[:n]` rather than an `ORDER BY`, so a grep for one found neither.

What this pins is not that the two backends return the same hits. They legitimately do not:
the twin scores full-text by raw token overlap while Postgres uses `to_tsquery('english')`,
which stems, so the same query can match different rows by design. What has to hold on both
is that the answer is *decided* — that a tie inside a channel, or between two fused
candidates, resolves the same way every time and the same way on either backend, because RRF
scores on position and a truncated channel is where an undecided tie turns into a different
answer rather than a differently-ordered one.

The corpus is chosen to avoid the stemming difference: every token is already its own stem,
so both backends see the same candidates and the contract is about order rather than recall.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.memory import recall as recall_mod
from felix.memory import store as memory_store

BACKENDS = ["memory", "postgres"]
parametrized = pytest.mark.parametrize("memory_settings", BACKENDS, indirect=True)

TENANT = "conformance"
MANIFEST = "m"


@pytest.fixture
def one_millisecond(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop the clock, so `created_at` ties and the id below it is what decides.

    `_rank` sorts on score, then recency, then id. Recency is `created_at`, so without a
    frozen clock the id tiebreak is never consulted and a test of it tests recency instead —
    the same trap the `list_active` cases fell into twice, once on each backend.
    """
    monkeypatch.setattr(memory_store, "now_ms", lambda: 1_700_000_000_000)


async def _put(settings: Any, content: str, **kw: Any) -> dict[str, Any]:
    return await memory_store.put_memory(settings, TENANT, content=content, manifest_id=MANIFEST, **kw)


async def _recall(settings: Any, query: str, **kw: Any) -> list[Any]:
    return await recall_mod.recall(settings, TENANT, query, manifest_id=MANIFEST, **kw)


# --- the answer is decided ------------------------------------------------------------------


@parametrized
@pytest.mark.asyncio
async def test_recall_is_deterministic(memory_settings: Any, one_millisecond: None) -> None:
    """The weakest property, and the one that was not true.

    Every candidate here scores identically: same token overlap, same kind, same default
    importance, same frozen `created_at`. So every key above the id ties, and before the id
    was added the order came from insertion on one backend and from the query plan on the
    other.
    """
    facts = [await _put(memory_settings, f"alpha beta gamma {i}") for i in range(6)]
    assert len({f["id"] for f in facts}) == 6, "the corpus collapsed; ids must be distinct"

    first = await _recall(memory_settings, "alpha beta", limit=3)
    again = await _recall(memory_settings, "alpha beta", limit=3)

    assert [h.id for h in first] == [h.id for h in again], "two identical recalls disagreed"
    assert len(first) == 3


@parametrized
@pytest.mark.asyncio
async def test_the_truncation_keeps_the_same_facts_every_time(
    memory_settings: Any, one_millisecond: None
) -> None:
    """Which facts survive the cut, not merely their order.

    `recall` is called with a `limit` and the caller sees only what survives it, so an
    undecided tie does not reorder an answer — it changes which memories the agent is given.
    """
    facts = [await _put(memory_settings, f"alpha beta gamma {i}") for i in range(6)]
    ids = sorted((f["id"] for f in facts), reverse=True)

    kept = {h.id for h in await _recall(memory_settings, "alpha beta", limit=3)}

    assert kept == set(ids[:3]), kept
    # Not the first three written, nor the last three, so a backend falling back to physical
    # order keeps a different set rather than the same set in a different order.
    written = [f["id"] for f in facts]
    assert kept not in ({*written[:3]}, {*written[-3:]}), "the corpus stopped discriminating"


@parametrized
@pytest.mark.asyncio
async def test_a_tie_inside_one_channel_resolves_the_same_way(
    memory_settings: Any, one_millisecond: None
) -> None:
    """The channel cut is upstream of fusion, and RRF scores on position.

    A channel that returns a different set of `per_channel` candidates does not shift a hit by
    one place — it changes what fusion is given. Over-fetching is `max(limit * 2, 10)`, so a
    corpus larger than that is what makes the channel's own truncation observable.
    """
    facts = [await _put(memory_settings, f"alpha beta gamma {i}") for i in range(14)]
    ids = sorted((f["id"] for f in facts), reverse=True)

    hits = await _recall(memory_settings, "alpha beta", limit=4)

    assert [h.id for h in hits] == ids[:4], [h.id for h in hits]


# --- the rest of the contract ---------------------------------------------------------------


@parametrized
@pytest.mark.asyncio
async def test_the_kind_filter_applies_on_both_arms(memory_settings: Any, one_millisecond: None) -> None:
    await _put(memory_settings, "alpha beta one", kind="fact")
    await _put(memory_settings, "alpha beta two", kind="preference")

    facts = await _recall(memory_settings, "alpha beta", kinds=["fact"])

    assert [h.kind for h in facts] == ["fact"]
    assert {h.kind for h in await _recall(memory_settings, "alpha beta")} == {"fact", "preference"}


@parametrized
@pytest.mark.asyncio
async def test_one_tenants_memories_are_not_recalled_for_another(memory_settings: Any) -> None:
    await _put(memory_settings, "alpha beta mine")
    await memory_store.put_memory(
        memory_settings, "other-tenant", content="alpha beta theirs", manifest_id=MANIFEST
    )

    hits = await _recall(memory_settings, "alpha beta")

    assert [h.content for h in hits] == ["alpha beta mine"]


@parametrized
@pytest.mark.asyncio
async def test_a_superseded_memory_is_not_recalled(memory_settings: Any) -> None:
    """`recall` filters on active status in the channel *and* again in `_rank`."""
    await _put(memory_settings, "timezone is utc", topic_key="user.timezone", origin_seq=1)
    await _put(memory_settings, "timezone is cet", topic_key="user.timezone", origin_seq=2)

    hits = await _recall(memory_settings, "timezone")

    assert [h.content for h in hits] == ["timezone is cet"], [h.content for h in hits]


@parametrized
@pytest.mark.asyncio
async def test_an_empty_query_recalls_nothing(memory_settings: Any) -> None:
    await _put(memory_settings, "alpha beta gamma")

    assert await _recall(memory_settings, "   ") == []
    assert await _recall(memory_settings, "") == []


@parametrized
@pytest.mark.asyncio
async def test_two_candidates_from_different_channels_resolve_by_id(
    memory_settings: Any, one_millisecond: None
) -> None:
    """The ranking pass's own tiebreak, which the channel fix alone does not pin.

    Once each channel orders its own ties, `fused` is deterministic — so a test with one
    channel cannot tell whether `_rank` breaks its ties or merely inherits that order. This
    is the case that can: two candidates matched by *different* channels, each at rank 0, so
    reciprocal-rank fusion scores them identically. With `kind`, `importance` and the frozen
    `created_at` equal too, every key above the id ties, and `fused` insertion order puts the
    full-text match first while the id puts the topic match first. Those disagree, so the
    tiebreak is the only thing deciding.
    """
    fts_hit = await _put(memory_settings, "alpha only 0")
    topic_hit = await _put(memory_settings, "zeta only 0", topic_key="alpha.thing")
    assert fts_hit["id"] < topic_hit["id"], "the corpus no longer distinguishes fusion order from id order"

    hits = await _recall(memory_settings, "alpha")

    assert [h.id for h in hits] == [topic_hit["id"], fts_hit["id"]], [h.content for h in hits]
