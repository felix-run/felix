"""What a chat turn costs the store in transactions, and the three things that keep it down.

A transaction is not one round trip: it is a pool checkout's ping, `BEGIN`, the RLS
`set_config` every transaction carries, the query, and `COMMIT`. Measured on a plain `quick`
turn against Postgres, `/chat` opened 13 and `/chat/stream` 15 -- around seventy round trips
before the model's first token on a managed database. Three of them read one `thread_state` row,
two re-read the head the turn's own appends had just written, and the render's two reads took a
transaction each.

The memory arm runs the behaviour each change must keep; the counts are Postgres-only, because
the twin has no transactions to count.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Any

import pytest
from felix.session.types import AppendableEvent

BACKENDS = ["memory", "postgres"]
TENANT = "conformance"

parametrized = pytest.mark.parametrize("store_settings", BACKENDS, indirect=True)
postgres_only = pytest.mark.parametrize("store_settings", ["postgres"], indirect=True)

# A plain turn on the `quick` manifest, one transaction each: the in-flight run check, the pin's
# `thread_state` read, the skill library, memory facts, the leaf, the render, and the user's and
# the reply's appends (`append_batch` and `store_leaf` apiece), and at the end the read for what a
# scheduled task reported while the turn ran (`idle_append.deliver_pending`). `store_leaf` is its own
# on purpose (a failed leaf write must not lose the events); so is that read, which the request's
# memo would answer from before a worker queued anything. Exact, per path: a saving updates the
# number here in the same diff, so a later regression cannot hide inside the slack it left.
TURN_TRANSACTIONS = {"/chat": 11, "/chat/stream": 11}


def _thread() -> str:
    return f"{TENANT}:{uuid.uuid4().hex}"


@contextmanager
def _counted_transactions() -> Iterator[list[str]]:
    """Every transaction any engine in the process begins: ORM sessions and Core connections alike."""
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    begun: list[str] = []

    def record(_connection: Any) -> None:
        begun.append("begin")

    event.listen(Engine, "begin", record)
    try:
        yield begun
    finally:
        event.remove(Engine, "begin", record)


@contextmanager
def _memoized_request(settings: Any) -> Iterator[Any]:
    from felix.context import AuthContext, RequestContext, run_with_context
    from felix.session.meta_memo import memoize_thread_meta

    with run_with_context(RequestContext(settings=settings, auth=AuthContext(tenant_id=TENANT))):
        yield memoize_thread_meta()


# --- the request memo ------------------------------------------------------------------------


@parametrized
async def test_a_memoized_request_sees_its_own_writes(store_settings: Any) -> None:
    """Read-your-writes inside a memoized request. On `memory://` there is no memo to consult --
    `get_thread_meta` answers from the twin's dict before reaching it -- so that arm is the
    behaviour the Postgres arm, which goes through the memo, has to match."""
    from felix.session.thread_state import (
        _default_meta,
        get_thread_meta,
        note_first_message,
        update_thread_meta,
    )

    thread = _thread()
    with _memoized_request(store_settings):
        meta = await get_thread_meta(settings=store_settings, tenant_id=TENANT, thread_id=thread)
        assert meta["thinking_level"] == _default_meta()["thinking_level"]
        await update_thread_meta(
            settings=store_settings, tenant_id=TENANT, thread_id=thread, thinking_level="high"
        )
        meta = await get_thread_meta(settings=store_settings, tenant_id=TENANT, thread_id=thread)
        assert meta["thinking_level"] == "high"
        assert await note_first_message(
            settings=store_settings, tenant_id=TENANT, thread_id=thread, text="hi"
        )
        meta = await get_thread_meta(settings=store_settings, tenant_id=TENANT, thread_id=thread)
        assert meta["preview"] == "hi"
        # And a caller mutating what it was handed does not reach the next reader -- a second read,
        # so on Postgres it is the memo's answer being mutated, not the read's own.
        meta = await get_thread_meta(settings=store_settings, tenant_id=TENANT, thread_id=thread)
        meta["thinking_level"] = "low"
        again = await get_thread_meta(settings=store_settings, tenant_id=TENANT, thread_id=thread)
        assert again["thinking_level"] == "high"


@postgres_only
async def test_a_memoized_request_reads_the_row_once(store_settings: Any) -> None:
    from felix.session.thread_state import get_thread_meta, note_first_message, update_thread_meta

    thread = _thread()
    await update_thread_meta(settings=store_settings, tenant_id=TENANT, thread_id=thread, session_name="n")
    await note_first_message(settings=store_settings, tenant_id=TENANT, thread_id=thread, text="first")

    async def three_reads() -> None:
        # The route's pin check, the loop's thinking level, and the preview check.
        await get_thread_meta(settings=store_settings, tenant_id=TENANT, thread_id=thread)
        await get_thread_meta(settings=store_settings, tenant_id=TENANT, thread_id=thread)
        await note_first_message(settings=store_settings, tenant_id=TENANT, thread_id=thread, text="later")

    with _counted_transactions() as begun:
        await three_reads()
    assert len(begun) == 3, "without a memo every read is a transaction, as before"

    with _memoized_request(store_settings), _counted_transactions() as begun:
        await three_reads()
        # The same thread id under another tenant is another row: the memo must not answer it.
        await get_thread_meta(settings=store_settings, tenant_id="another", thread_id=thread)
    assert len(begun) == 2, begun


# --- the live stream's cursor ----------------------------------------------------------------


@parametrized
async def test_the_known_head_is_the_stores_head(store_settings: Any) -> None:
    from felix.session.heads import known_head
    from felix.session.store import get_session_store

    thread = _thread()
    session = get_session_store(store_settings, tenant_id=TENANT).open(thread)
    assert known_head(TENANT, thread) is None
    await session.append_batch([AppendableEvent(kind="message", role="user", content="a")])
    await session.append_batch(
        [
            AppendableEvent(kind="message", role="assistant", content="b"),
            AppendableEvent(kind="message", role="user", content="c"),
        ]
    )
    assert known_head(TENANT, thread) == (await session.head())["seq"] == 3
    assert known_head("another", thread) is None, "a head is per tenant, as the log is"
    await session.reset()
    # After a reset the store's head is zero again; a remembered 3 would skip the next events.
    assert known_head(TENANT, thread) is None


# --- the render's reads ----------------------------------------------------------------------


@parametrized
async def test_reads_inside_reading_answer_as_outside(store_settings: Any) -> None:
    from felix.session.store import get_session_store
    from felix.session.types import GetEventsOpts

    thread = _thread()
    session = get_session_store(store_settings, tenant_id=TENANT).open(thread)
    await session.append_batch(
        [AppendableEvent(kind="message", role="user", content=str(i)) for i in range(4)]
    )
    outside = (await session.get_event_skeletons(), await session.get_events(GetEventsOpts(from_seq=2)))
    async with session.reading():
        inside = (await session.get_event_skeletons(), await session.get_events(GetEventsOpts(from_seq=2)))
    assert inside == outside


@postgres_only
async def test_reading_shares_one_transaction_with_its_own_thread_only(store_settings: Any) -> None:
    from felix.session.store import get_session_store

    store = get_session_store(store_settings, tenant_id=TENANT)
    mine, other = store.open(_thread()), store.open(_thread())
    await mine.append_batch([AppendableEvent(kind="message", role="user", content="x")])

    with _counted_transactions() as begun:
        async with mine.reading():
            await mine.get_event_skeletons()
            await mine.get_events()
            await mine.get_events()
    assert len(begun) == 1, begun

    with _counted_transactions() as begun:
        async with mine.reading():
            await mine.get_events()
            # Another thread's read inside the scope takes its own transaction, not this one.
            assert await other.get_events() == []
    assert len(begun) == 2, begun


# --- a whole turn ----------------------------------------------------------------------------


@asynccontextmanager
async def _booted(settings: Any, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Any]:
    """`create_application()` on this arm's database, with the scripted model on every route."""
    from felix.config import get_settings
    from felix.patterns.model_registry import register_model_provider
    from felix_ai.providers.scripted import ScriptedClient, ScriptedTurn
    from httpx import ASGITransport, AsyncClient

    from tests.support.e2e import (
        DEFAULT_ROUTE,
        assert_routes_to_the_script,
        provider_registry,
        scripted_model_routes,
    )

    for key, value in {
        "FELIX_DATABASE_URL": settings.database_url,
        "FELIX_DEFAULT_MODEL_ID": DEFAULT_ROUTE,
        "FELIX_MODEL_ROUTES": json.dumps(scripted_model_routes()),
        "FELIX_ANTHROPIC_API_KEY": "",
        "FELIX_OPENAI_API_KEY": "",
        "FELIX_MODEL_PROVIDER_OPTIONS": "",
        "FELIX_REDIS_URL": "",
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    saved = dict(provider_registry())

    def factory(model_id: str, route: Any, spec: Any, settings: Any) -> ScriptedClient:
        return ScriptedClient(model_id=model_id, route=route, script=[ScriptedTurn(content="ok")] * 4)

    register_model_provider("scripted", factory)
    try:
        from felix_api.main import create_application

        app = create_application()
        assert_routes_to_the_script(app.state.settings)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://felix.test", timeout=30
        ) as client:
            yield client
    finally:
        registry = provider_registry()
        registry.clear()
        registry.update(saved)
        get_settings.cache_clear()


@postgres_only
@pytest.mark.parametrize("path", ["/chat", "/chat/stream"])
async def test_a_plain_turn_stays_inside_its_transaction_budget(
    store_settings: Any, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    thread = _thread().split(":", 1)[1]

    async def turn(text: str) -> None:
        body = {"manifest": "quick", "thread_id": thread, "messages": [{"role": "user", "content": text}]}
        response = await client.post(path, json=body)
        assert response.status_code == 200, response.text[:300]
        await response.aread()

    async with _booted(store_settings, monkeypatch) as client:
        # The first turn creates what later ones only read: the row, the preview, the leaf mark.
        await turn("one")
        with _counted_transactions() as begun:
            await turn("two")
    expected = TURN_TRANSACTIONS[path]
    assert len(begun) == expected, (
        f"{path} took {len(begun)} transactions on a plain turn, not {expected}. More is a "
        "regression: each costs a ping, BEGIN, set_config and COMMIT in round trips. Fewer is a "
        "saving: lower TURN_TRANSACTIONS in this diff, so the slack cannot hide the next regression."
    )
