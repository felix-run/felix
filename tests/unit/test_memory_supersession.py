"""Supersession, provenance, and as-of over the memory store.

The turn-versioning columns existed before this and nothing populated or queried
them: `capture_from_turn` never passed `origin_seq`, nothing ever passed
`supersedes_id`, there was no `topic_key` to key supersession on, and
`consolidate_pools` wrote a millisecond timestamp into the ordinal column. These
assert the columns now mean what they claim.
"""

from __future__ import annotations

import pytest
from felix.config import Settings
from felix.memory import store as memory_store
from felix.memory.store import ACTIVE, FORGOTTEN, SUPERSEDED

TENANT = "t-mem"
MANIFEST = "m"


@pytest.fixture(autouse=True)
def _clean() -> None:
    memory_store._memory_rows.clear()


def _settings() -> Settings:
    return Settings(database_url="memory://test")


async def _put(content: str, **kw):
    return await memory_store.put_memory(_settings(), TENANT, content=content, manifest_id=MANIFEST, **kw)


@pytest.mark.asyncio
async def test_same_content_collapses_to_one_row() -> None:
    """Content-addressed ids: storing a fact twice must not double it in recall."""
    a = await _put("The user prefers dark mode.")
    b = await _put("the   user prefers DARK mode.")  # same sentence, different rendering
    assert a["id"] == b["id"]
    active = await memory_store.list_active(_settings(), TENANT, manifest_id=MANIFEST)
    assert len(active) == 1


@pytest.mark.asyncio
async def test_content_hash_is_scoped_by_manifest() -> None:
    """The PK is (tenant, id), so hashing content alone would cross-wire two agents."""
    s = _settings()
    one = await memory_store.put_memory(s, TENANT, content="same text", manifest_id="agent-a")
    two = await memory_store.put_memory(s, TENANT, content="same text", manifest_id="agent-b")
    assert one["id"] != two["id"]


@pytest.mark.asyncio
async def test_topic_key_supersedes_the_previous_value() -> None:
    old = await _put("Timezone is UTC.", topic_key="user.timezone", origin_seq=4)
    new = await _put("Timezone is CET.", topic_key="user.timezone", origin_seq=7)

    rows = await memory_store.get_many(_settings(), TENANT, [old["id"], new["id"]])
    assert rows[old["id"]]["status"] == SUPERSEDED
    assert rows[old["id"]]["superseded_by"] == new["id"]
    assert rows[new["id"]]["status"] == ACTIVE

    active = await memory_store.list_active(_settings(), TENANT, manifest_id=MANIFEST)
    assert [r["content"] for r in active] == ["Timezone is CET."]


@pytest.mark.asyncio
async def test_supersession_closes_the_interval_at_the_new_turn() -> None:
    """Not the old row's ordinal — the interval ends when the replacement arrived."""
    old = await _put("Timezone is UTC.", topic_key="user.timezone", origin_seq=4)
    await _put("Timezone is CET.", topic_key="user.timezone", origin_seq=7)
    rows = await memory_store.get_many(_settings(), TENANT, [old["id"]])
    assert rows[old["id"]]["superseded_seq"] == 7


@pytest.mark.asyncio
async def test_as_of_shows_what_was_believed_then() -> None:
    """The point of turn-versioning: a superseded fact is still visible in its own era."""
    await _put("Timezone is UTC.", topic_key="user.timezone", origin_seq=4)
    await _put("Timezone is CET.", topic_key="user.timezone", origin_seq=7)

    at5 = await memory_store.as_of(_settings(), TENANT, 5, manifest_id=MANIFEST)
    at9 = await memory_store.as_of(_settings(), TENANT, 9, manifest_id=MANIFEST)
    assert [r["content"] for r in at5] == ["Timezone is UTC."]
    assert [r["content"] for r in at9] == ["Timezone is CET."]


@pytest.mark.asyncio
async def test_as_of_before_a_fact_existed_excludes_it() -> None:
    await _put("Learned at turn 6.", origin_seq=6)
    assert await memory_store.as_of(_settings(), TENANT, 3, manifest_id=MANIFEST) == []


@pytest.mark.asyncio
async def test_rows_without_provenance_read_as_genesis() -> None:
    """Rows written before provenance existed must not vanish from every as-of view."""
    await _put("Ancient fact.", origin_seq=None)
    at0 = await memory_store.as_of(_settings(), TENANT, 0, manifest_id=MANIFEST)
    assert [r["content"] for r in at0] == ["Ancient fact."]


@pytest.mark.asyncio
async def test_re_remembering_keeps_the_original_provenance() -> None:
    """Reactivation must not rewrite when the fact was first learned."""
    first = await _put("Stable fact.", origin_seq=2)
    await memory_store.forget(_settings(), TENANT, first["id"])
    again = await _put("Stable fact.", origin_seq=9)

    rows = await memory_store.get_many(_settings(), TENANT, [again["id"]])
    assert rows[again["id"]]["status"] == ACTIVE
    assert rows[again["id"]]["origin_seq"] == 2, "provenance was rewritten by a later write"


@pytest.mark.asyncio
async def test_forget_hides_without_deleting() -> None:
    row = await _put("Regrettable fact.")
    assert await memory_store.forget(_settings(), TENANT, row["id"]) is True
    assert await memory_store.list_active(_settings(), TENANT, manifest_id=MANIFEST) == []
    still_there = await memory_store.get_many(_settings(), TENANT, [row["id"]])
    assert still_there[row["id"]]["status"] == FORGOTTEN


@pytest.mark.asyncio
async def test_forget_has_no_turn_endpoint() -> None:
    """An operator decision is not something a turn did, so it is not a supersession."""
    row = await _put("Regrettable fact.", origin_seq=3)
    await memory_store.forget(_settings(), TENANT, row["id"])
    rows = await memory_store.get_many(_settings(), TENANT, [row["id"]])
    assert rows[row["id"]]["superseded_seq"] is None


@pytest.mark.asyncio
async def test_consolidate_never_writes_a_timestamp_into_the_ordinal() -> None:
    """The bug this replaces: `superseded_seq = origin_seq or now_ms()`.

    A millisecond epoch in a turn-ordinal column makes every later as-of comparison
    wrong by thirteen orders of magnitude.
    """
    s = _settings()
    # Two rows with the same content under one manifest, which content-hash ids would
    # normally collapse — so write them directly, as pre-hash rows would have been.
    for i in range(2):
        memory_store._memory_rows[(TENANT, f"legacy-{i}")] = {
            "tenant_id": TENANT,
            "id": f"legacy-{i}",
            "kind": "fact",
            "manifest_id": MANIFEST,
            "content": "duplicated fact",
            "metadata": {},
            "created_at": 1000 + i,
            "status": ACTIVE,
            "origin_seq": 3,
            "superseded_seq": None,
        }

    assert await memory_store.consolidate_pools(s) == 1
    seqs = [r.get("superseded_seq") for r in memory_store._memory_rows.values()]
    assert all(v is None or v < 1_000_000 for v in seqs), f"timestamp leaked into ordinal: {seqs}"


@pytest.mark.asyncio
async def test_current_turn_seq_tracks_the_highest_ordinal() -> None:
    s = _settings()
    assert await memory_store.current_turn_seq(s, TENANT, manifest_id=MANIFEST) == 0
    await _put("a", origin_seq=3)
    await _put("b", origin_seq=11)
    assert await memory_store.current_turn_seq(s, TENANT, manifest_id=MANIFEST) == 11


@pytest.mark.asyncio
async def test_thread_id_is_recorded_but_does_not_scope_recall() -> None:
    """Provenance, not a filter — a fact learned in one thread is still a fact."""
    await _put("Learned in thread one.", thread_id="thread-1")
    active = await memory_store.list_active(_settings(), TENANT, manifest_id=MANIFEST)
    assert len(active) == 1
    assert active[0]["thread_id"] == "thread-1"


# --- the wiring, not just the store ---------------------------------------------
#
# The store having provenance columns is worth nothing if the turn never supplies an
# ordinal, which is exactly the state this replaces: `capture_from_turn` had no
# `origin_seq` parameter, so every fact the system stored had a null one.


@pytest.mark.asyncio
async def test_capture_stamps_the_turn_ordinal() -> None:
    from felix.manifests.schema import MemoryCapture
    from felix.memory.capture import capture_from_turn

    settings = Settings(database_url="memory://cap", object_store="memory", allow_insecure=True)
    stored = await capture_from_turn(
        settings,
        TENANT,
        manifest_id=MANIFEST,
        user_text="Remember this.",
        assistant_text="The API base URL is https://example.com/v1.",
        capture=MemoryCapture(enabled=True, max_facts=5, min_chars=10),
        model=None,
        origin_seq=12,
        thread_id="thread-9",
    )
    assert stored

    rows = await memory_store.list_active(settings, TENANT, manifest_id=MANIFEST, kind="fact")
    assert rows
    assert all(r["origin_seq"] == 12 for r in rows), "facts stored without provenance"
    assert all(r["thread_id"] == "thread-9" for r in rows)


@pytest.mark.asyncio
async def test_every_fact_from_one_turn_shares_an_ordinal() -> None:
    """Otherwise an as-of reconstruction would see one turn's facts arrive separately."""
    from felix.manifests.schema import MemoryCapture
    from felix.memory.capture import capture_from_turn

    settings = Settings(database_url="memory://cap", object_store="memory", allow_insecure=True)
    await capture_from_turn(
        settings,
        TENANT,
        manifest_id=MANIFEST,
        user_text="Remember these.",
        assistant_text=(
            "The API base URL is https://example.com/v1.\n"
            "The default region is eu-west-1.\n"
            "The retry budget is three attempts."
        ),
        capture=MemoryCapture(enabled=True, max_facts=5, min_chars=10),
        model=None,
        origin_seq=20,
    )
    rows = await memory_store.list_active(settings, TENANT, manifest_id=MANIFEST, kind="fact")
    assert len(rows) > 1, "need several facts for this to mean anything"
    assert {r["origin_seq"] for r in rows} == {20}


@pytest.mark.asyncio
async def test_react_reads_the_turn_ordinal_from_the_session_log() -> None:
    """The session's own `seq` is the turn clock; there is no second counter."""
    from felix.patterns.react import build_react_agent
    from felix.session.store import InMemorySessionStore
    from felix.session.types import AppendableEvent

    store = InMemorySessionStore()
    agent = build_react_agent(
        {
            "tools": [],
            "manifest_id": MANIFEST,
            "system_prompt": "sp",
            "recursion_limit": 3,
            "session_store": store,
        }
    )
    session = store.open("thread-seq")
    for text in ("one", "two", "three"):
        await session.append(AppendableEvent(kind="message", role="user", content=text))

    assert await agent._turn_seq("thread-seq") == 3
    assert await agent._turn_seq(None) is None
