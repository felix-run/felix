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

The corpus is chosen so the stemming difference cannot bite. Mostly that is because a token
is already its own stem (`alpha`, `beta`, `gamma`, `zeta`); `timezone` is not — it stems to
`timezon` — and is safe for the different reason that the stemming is *symmetric*, applied to
the content and the query alike, so both still match. A word like `timezones` would not be.
Checked against a real `to_tsquery('english', ...)` rather than assumed.

Two more differences the corpus stays clear of rather than resolves: English stopwords
(`is`, `only`, `theirs`) vanish from `content_tsv` but survive the twin's tokeniser, and the
twin drops tokens of two characters or fewer where Postgres keeps them. None is a query token
here.
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


class _OneAxisEmbedder:
    """Deterministic embeddings, no model and no network.

    Sized to the real column — `vector(768)` since 0001_baseline — with every text mapped to
    the same unit vector, so cosine distance ties *exactly* across the corpus. That is the
    point: the vector channel orders by distance, and a tie there is what the id below it
    resolves. Real embeddings tie rarely; the channel's truncation still has to be decided.
    """

    enabled = True
    dim = 768

    async def embed(self, texts: Any) -> list[list[float]]:
        return [[1.0, 0.0] + [0.0] * (self.dim - 2) for _ in texts]


async def _put(settings: Any, content: str, **kw: Any) -> dict[str, Any]:
    return await memory_store.put_memory(settings, TENANT, content=content, manifest_id=MANIFEST, **kw)


async def _recall(settings: Any, query: str, **kw: Any) -> list[Any]:
    return await recall_mod.recall(settings, TENANT, query, manifest_id=MANIFEST, **kw)


# --- the answer is decided ------------------------------------------------------------------


@parametrized
@pytest.mark.asyncio
async def test_the_answer_does_not_depend_on_the_order_facts_were_written(
    memory_settings: Any, one_millisecond: None
) -> None:
    """Two identical calls agreeing proves nothing, so this writes the corpus twice.

    Within one process the twin's dict iteration is fixed and Postgres answers two identical
    statements from one plan, so back-to-back calls agree whatever the sort key is — the
    version of this test that asserted that was green under every pre-fix variant. Writing
    the same facts in the opposite order is what makes insertion order differ, which is the
    fallback the channels used to have.
    """
    forwards = [await _put(memory_settings, f"alpha beta gamma {i}") for i in range(6)]
    first = await _recall(memory_settings, "alpha beta", limit=3)

    memory_settings_second = memory_settings
    for fact in forwards:
        await memory_store.forget(memory_settings_second, TENANT, fact["id"])
    backwards = [await _put(memory_settings, f"alpha beta gamma {i}") for i in reversed(range(6))]

    second = await _recall(memory_settings, "alpha beta", limit=3)

    assert {f["id"] for f in backwards} == {f["id"] for f in forwards}, "the rewrite changed the corpus"
    assert [h.id for h in second] == [h.id for h in first], "the answer depended on write order"


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
async def test_the_kind_filter_applies_before_the_channel_cut(
    memory_settings: Any, one_millisecond: None
) -> None:
    """A corpus larger than the over-fetch window, because that is where this broke.

    `kinds` used to be applied only in `_rank`, after each channel had been cut to
    `per_channel` — so a matching memory outside the window was filtered against an answer it
    had already been excluded from, and `recall(kinds=["fact"])` returned nothing while the
    fact was stored and active. Reproduced on the twin with twenty `event` rows and one
    `fact`; a two-row corpus sits inside the window and cannot fail on it.

    The `fact` here is written *last* and carries the default importance, so nothing but the
    filter can keep it: it is the least preferred row in the channel's own tie order.
    """
    for i in range(20):
        await _put(memory_settings, f"alpha beta note {i}", kind="event")
    curated = await _put(memory_settings, "alpha beta curated 7", kind="fact")

    facts = await _recall(memory_settings, "alpha beta", kinds=["fact"])

    assert [h.id for h in facts] == [curated["id"]], [h.content for h in facts]
    assert {h.kind for h in await _recall(memory_settings, "alpha beta", limit=25)} == {"event", "fact"}


@parametrized
@pytest.mark.asyncio
async def test_recall_is_scoped_to_one_manifest_and_can_span_them(
    memory_settings: Any, one_millisecond: None
) -> None:
    """The branch production takes, which nothing exercised.

    `GET /memory/search` declares `manifest_id: str = ""`, so the default is to recall across
    *every* manifest a tenant has — and `put_memory` hashes the manifest into the id precisely
    so one tenant can hold two. Every other case here routes through a helper that hard-codes
    one manifest, so the predicate never excluded anything and never had to include anything.
    """
    mine = await _put(memory_settings, "alpha beta shared wording")
    theirs = await memory_store.put_memory(
        memory_settings, TENANT, content="alpha beta shared wording", manifest_id="other"
    )
    assert mine["id"] != theirs["id"], "the manifest is supposed to be part of the id"

    scoped = await recall_mod.recall(memory_settings, TENANT, "alpha beta", manifest_id=MANIFEST)
    across = await recall_mod.recall(memory_settings, TENANT, "alpha beta", manifest_id="")

    assert [h.id for h in scoped] == [mine["id"]]
    assert {h.id for h in across} == {mine["id"], theirs["id"]}


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


@parametrized
@pytest.mark.asyncio
async def test_the_vector_channel_truncates_by_the_same_total_order(
    memory_settings: Any, one_millisecond: None
) -> None:
    """The third channel, which no other case reaches.

    Nothing else in this file passes an `embedder`, so `_embed_query` returns `None` and the
    vector channel is skipped entirely — two of three channels were covered while the
    docstrings said three. Its `ORDER BY` mixes directions deliberately (distance ascending,
    id descending), which is worth exercising rather than reasoning about.

    Every fact here embeds to the same unit vector, so the distances tie exactly and the id is
    the only thing left to decide which survive `per_channel` and then `limit`.
    """
    embedder = _OneAxisEmbedder()
    vectors = await embedder.embed([""] * 6)
    facts = [await _put(memory_settings, f"unrelated wording {i}", embedding=vectors[i]) for i in range(6)]
    ids = sorted((f["id"] for f in facts), reverse=True)

    # A query sharing no token with any content, so only the vector channel can match.
    hits = await _recall(memory_settings, "quixotic", limit=3, embedder=embedder)

    assert [h.id for h in hits] == ids[:3], [h.content for h in hits]
    assert all("vector" in h.channels for h in hits), [h.channels for h in hits]
    written = [f["id"] for f in facts]
    assert {*ids[:3]} not in ({*written[:3]}, {*written[-3:]}), "the corpus stopped discriminating"
