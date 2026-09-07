"""One contract for session search, run against both backends.

The in-memory index had no production writer at all until recently: it was filled by a single
unit test that seeded it by hand and then queried it, so what was tested was the query function
rather than the wiring, and `GET /chat/sessions/search` returned nothing for anything the
product had stored. Giving it a writer closed that, and nothing then compared the twin against
the Postgres `content_tsv` column it stands in for.

The two arms are genuinely different engines — a lowercase substring scan against
`plainto_tsquery('english', ...)` — so this contract deliberately asserts only what both can be
held to: that an appended event becomes findable, that deletion removes it, that the tenant and
thread boundaries hold, and that masking survives into the index. Ranking and stemming are named
here as *not* covered rather than left for a reader to assume — as is the hit shape: the
Postgres arm returns a `rank` key the twin never produces, and that difference is visible
through `GET /chat/sessions/search`.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.session.search import search_sessions
from felix.session.store import get_session_store
from felix.session.types import AppendableEvent

BACKENDS = ["memory", "postgres"]
parametrized = pytest.mark.parametrize("store_settings", BACKENDS, indirect=True)

TENANT = "conformance"


async def _append(settings: Any, thread: str, *texts: str, tenant: str = TENANT) -> None:
    session = get_session_store(settings, tenant_id=tenant).open(thread)
    for text in texts:
        await session.append(AppendableEvent(kind="message", role="user", content=text))


def _contents(hits: list[dict[str, Any]]) -> list[str]:
    return [str(hit.get("content") or "") for hit in hits]


# --- the property that was absent entirely --------------------------------------------------


@parametrized
@pytest.mark.asyncio
async def test_an_appended_event_becomes_findable(store_settings: Any) -> None:
    """On Postgres this is free — `content_tsv` is generated. On the twin it needs a writer."""
    await _append(store_settings, "t1", "the zucchini marker")

    hits = await search_sessions(store_settings, TENANT, "zucchini")
    assert _contents(hits) == ["the zucchini marker"], hits


@parametrized
@pytest.mark.asyncio
async def test_a_term_that_appears_nowhere_finds_nothing(store_settings: Any) -> None:
    """The other half: a search returning everything would satisfy the test above.

    The positive assertion is not decoration. `search_sessions` swallows every exception and
    answers `[]`, so a negative-only test passes against a search that is completely dead —
    which is close to the state this whole contract exists because of.
    """
    await _append(store_settings, "t1", "the zucchini marker")

    assert _contents(await search_sessions(store_settings, TENANT, "zucchini")) == ["the zucchini marker"]
    assert await search_sessions(store_settings, TENANT, "aubergine") == []


@parametrized
@pytest.mark.asyncio
async def test_an_empty_query_finds_nothing(store_settings: Any) -> None:
    """A blank box in the UI must not dump the whole log."""
    await _append(store_settings, "t1", "the zucchini marker")

    # Same reasoning as above: prove the index answers at all before asserting it stays quiet.
    assert await search_sessions(store_settings, TENANT, "zucchini")
    assert await search_sessions(store_settings, TENANT, "") == []
    assert await search_sessions(store_settings, TENANT, "   ") == []


# --- boundaries -----------------------------------------------------------------------------


@parametrized
@pytest.mark.asyncio
async def test_search_does_not_cross_the_tenant_boundary(store_settings: Any) -> None:
    """Both arms are asserted positively, so a total read outage cannot pass for isolation."""
    await _append(store_settings, "shared-name", "acme's zucchini", tenant=TENANT)
    await _append(store_settings, "shared-name", "globex's zucchini", tenant="other")

    assert _contents(await search_sessions(store_settings, TENANT, "zucchini")) == ["acme's zucchini"]
    assert _contents(await search_sessions(store_settings, "other", "zucchini")) == ["globex's zucchini"]


@parametrized
@pytest.mark.asyncio
async def test_a_hit_names_the_thread_it_came_from(store_settings: Any) -> None:
    """The hit is a deep link, so the thread id is the part a client needs to be right."""
    await _append(store_settings, "t1", "zucchini here")
    await _append(store_settings, "t2", "zucchini there")

    hits = await search_sessions(store_settings, TENANT, "zucchini")
    by_thread = {hit["thread_id"]: hit["content"] for hit in hits}
    assert by_thread == {"t1": "zucchini here", "t2": "zucchini there"}, hits


@parametrized
@pytest.mark.asyncio
async def test_the_limit_is_honoured(store_settings: Any) -> None:
    await _append(store_settings, "t1", *[f"zucchini {i}" for i in range(5)])

    assert len(await search_sessions(store_settings, TENANT, "zucchini", limit=2)) == 2


# --- deletion -------------------------------------------------------------------------------


@parametrized
@pytest.mark.asyncio
async def test_resetting_a_thread_removes_it_from_the_index(store_settings: Any) -> None:
    """`reset()` is what `DELETE /chat/history/{id}` and the retention sweep both reach.

    On Postgres the generated column dies with the row. The twin has to be told, and a delete
    that leaves the text findable is a delete that did not happen.
    """
    store = get_session_store(store_settings, tenant_id=TENANT)
    session = store.open("t1")
    await session.append(AppendableEvent(kind="message", role="user", content="findable"))
    assert await search_sessions(store_settings, TENANT, "findable")

    await session.reset()

    assert await search_sessions(store_settings, TENANT, "findable") == []


@parametrized
@pytest.mark.asyncio
async def test_deleting_one_thread_leaves_the_others_findable(store_settings: Any) -> None:
    """The blast radius of a delete, expressed as the behaviour rather than the mechanism.

    Driven through `reset()` rather than the in-memory `drop_thread_index`, which has no
    Postgres counterpart at all — there the rows simply go. A contract written against the
    twin's internals would pass on memory and be meaningless on the backend it stands in for.
    """
    await _append(store_settings, "keep", "zucchini kept")
    await _append(store_settings, "drop", "zucchini dropped")

    await get_session_store(store_settings, tenant_id=TENANT).open("drop").reset()

    assert _contents(await search_sessions(store_settings, TENANT, "zucchini")) == ["zucchini kept"]


@parametrized
@pytest.mark.asyncio
async def test_deleting_a_thread_does_not_reach_another_tenant(store_settings: Any) -> None:
    """Thread ids are namespaced per tenant; a delete keyed on the id alone would cross that."""
    await _append(store_settings, "shared-name", "acme's zucchini", tenant=TENANT)
    await _append(store_settings, "shared-name", "globex's zucchini", tenant="other")

    await get_session_store(store_settings, tenant_id=TENANT).open("shared-name").reset()

    assert await search_sessions(store_settings, TENANT, "zucchini") == []
    assert _contents(await search_sessions(store_settings, "other", "zucchini")) == ["globex's zucchini"]


# --- masking --------------------------------------------------------------------------------


@parametrized
@pytest.mark.asyncio
async def test_a_masked_secret_is_not_searchable(store_settings: Any) -> None:
    """The index is a second copy of event content, so it inherits the masking rule.

    Both arms index the string the row stores rather than the string the caller passed, so this
    holds by construction on each — which is exactly the kind of by-construction claim that
    stops being true when someone changes one of the two writers.
    """
    import felix.secrets as secrets_mod

    secret = "super-secret-value-9f2b"
    original = secrets_mod.collected_secret_values
    secrets_mod.collected_secret_values = lambda *a, **k: [secret]  # type: ignore[assignment]
    try:
        await _append(store_settings, "t1", f"the key is {secret}")

        assert await search_sessions(store_settings, TENANT, secret) == []
        masked = await search_sessions(store_settings, TENANT, "REDACTED")
        assert masked, "the event must still be indexed, in its masked form"
        assert all(secret not in (hit.get("content") or "") for hit in masked), masked
    finally:
        secrets_mod.collected_secret_values = original  # type: ignore[assignment]
