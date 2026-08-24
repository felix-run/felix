"""Reconnecting to a thread after a dropped stream.

A client that loses `POST /chat/stream` mid-turn had nothing to come back to: no `id:`
on any frame, and no route to reconnect to. The run itself is still torn down on
disconnect — deliberately, so a hung-up client stops burning tokens — so what a
reconnect recovers is the thread as it now stands, not the abandoned turn.

Every `id:` on both streams means the same thing: the next session sequence to ask
for. A client hands it straight back as `Last-Event-ID`.
"""

from __future__ import annotations

import json

import pytest
from felix.config import Settings
from felix.session.store import get_session_store
from felix.session.types import AppendableEvent
from httpx import ASGITransport, AsyncClient

THREAD = "resume-thread"


@pytest.fixture(autouse=True)
def _clean() -> None:
    """The in-memory session store is a module global, so threads leak between tests."""
    from felix.session import store as session_store

    session_store._memory_session_store._sessions.clear()


def _settings() -> Settings:
    return Settings(
        allow_insecure=True,
        auth_mode="none",
        environment="development",
        object_store="memory",
        database_url="memory://resume",
        host="127.0.0.1",
        # No Redis in the unit environment; the snapshot path consults it for lease
        # and steer state and would otherwise spend the test retrying a refused port.
        redis_url="",
        # Close the idle connection quickly so a wrong expectation fails fast rather
        # than holding the stream open for the production five minutes.
        stream_resume_idle_seconds=1.5,
        stream_resume_poll_seconds=0.05,
    )


def _client(settings: Settings) -> AsyncClient:
    from felix_api.app import create_app

    app = create_app(settings=settings, plugins=[])
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", timeout=30.0)


async def _seed(settings: Settings, *texts: str) -> None:
    # Threads are namespaced per tenant on the way in, so seed the id the route will
    # actually open rather than the bare one the client sends.
    from felix_api.threads import effective_thread_id

    thread = effective_thread_id("default", THREAD)
    assert thread is not None
    session = get_session_store(settings, tenant_id="default").open(thread)
    for text in texts:
        await session.append(AppendableEvent(kind="message", role="user", content=text))


def _frames(body: str) -> list[dict]:
    """Parse an SSE body into id/payload pairs, ignoring comments and [DONE]."""
    out: list[dict] = []
    for block in body.split("\n\n"):
        if not block.strip() or block.lstrip().startswith(":"):
            continue
        event_id, data = None, None
        for line in block.splitlines():
            if line.startswith("id: "):
                event_id = int(line[4:])
            elif line.startswith("data: "):
                data = line[6:]
        if data and data != "[DONE]":
            out.append({"id": event_id, "payload": json.loads(data)})
    return out


async def _read(client: AsyncClient, url: str, *, headers: dict | None = None, want: int = 1) -> str:
    """Read until `want` data frames have arrived, then stop.

    Bounded on purpose: the route holds the connection open and sends keep-alives, so
    an expectation that never becomes true would hang until the idle timeout instead
    of failing.
    """
    body = ""
    async with client.stream("GET", url, headers=headers or {}) as resp:
        assert resp.status_code == 200, resp.status_code
        assert resp.headers["content-type"].startswith("text/event-stream")
        async for chunk in resp.aiter_text():
            body += chunk
            if len(_frames(body)) >= want or len(body) > 200_000:
                break
    return body


@pytest.mark.asyncio
async def test_cold_reconnect_opens_with_a_snapshot() -> None:
    """No cursor means the client has nothing; give it the thread as it stands."""
    settings = _settings()
    await _seed(settings, "first", "second")

    async with _client(settings) as client:
        frames = _frames(await _read(client, f"/chat/stream/{THREAD}"))

    assert frames[0]["payload"]["event"] == "snapshot"
    assert frames[0]["id"] == 2, "the cursor must be the next sequence, not the last one"


@pytest.mark.asyncio
async def test_warm_reconnect_replays_only_what_was_missed() -> None:
    """The point of `Last-Event-ID`: do not re-send what the client already has."""
    settings = _settings()
    await _seed(settings, "one", "two", "three")

    async with _client(settings) as client:
        body = await _read(client, f"/chat/stream/{THREAD}", headers={"Last-Event-ID": "2"})

    frames = _frames(body)
    assert frames, "nothing was replayed"
    assert not any(f["payload"]["event"] == "snapshot" for f in frames), (
        "a warm reconnect resent the whole transcript"
    )
    contents = [f["payload"]["data"]["content"] for f in frames]
    assert contents == ["three"], f"expected only the missed event, got {contents}"


@pytest.mark.asyncio
async def test_the_cursor_a_reconnect_returns_is_usable_again() -> None:
    """Round trip: the id from one connection resumes correctly on the next."""
    settings = _settings()
    await _seed(settings, "one", "two")

    async with _client(settings) as client:
        first = _frames(await _read(client, f"/chat/stream/{THREAD}"))
        cursor = first[0]["id"]

        await _seed(settings, "three")
        body = await _read(client, f"/chat/stream/{THREAD}", headers={"Last-Event-ID": str(cursor)})

    contents = [f["payload"]["data"]["content"] for f in _frames(body)]
    assert contents == ["three"]


@pytest.mark.asyncio
async def test_last_event_id_also_accepted_as_a_query_parameter() -> None:
    """EventSource sets the header; a plain fetch or curl cannot."""
    settings = _settings()
    await _seed(settings, "one", "two")

    async with _client(settings) as client:
        frames = _frames(await _read(client, f"/chat/stream/{THREAD}?last_event_id=1"))

    assert not any(f["payload"]["event"] == "snapshot" for f in frames)
    assert [f["payload"]["data"]["content"] for f in frames] == ["two"]


@pytest.mark.asyncio
async def test_a_garbage_cursor_degrades_to_a_snapshot() -> None:
    """A malformed `Last-Event-ID` must not 500 a recovery surface."""
    settings = _settings()
    await _seed(settings, "one")

    async with _client(settings) as client:
        frames = _frames(
            await _read(client, f"/chat/stream/{THREAD}", headers={"Last-Event-ID": "not-a-number"})
        )

    assert frames[0]["payload"]["event"] == "snapshot"


@pytest.mark.asyncio
async def test_an_invalid_thread_id_is_rejected() -> None:
    async with _client(_settings()) as client:
        resp = await client.get("/chat/stream/..%2Fetc%2Fpasswd")
        assert resp.status_code in (400, 404)


# --- the wire contract ------------------------------------------------------------


def test_resume_points_exclude_the_per_token_frames() -> None:
    """Stamping every frame would cost a session-log query per token.

    Frames without an `id:` leave `lastEventId` untouched, so deltas inheriting the
    last structural cursor is correct SSE rather than a shortcut.
    """
    from felix_api.routes.chat import _RESUME_POINTS

    assert "text_delta" not in _RESUME_POINTS
    assert "session_progress" not in _RESUME_POINTS
    assert {"done", "tool_end"} <= _RESUME_POINTS


@pytest.mark.asyncio
async def test_stream_cursor_is_the_next_sequence() -> None:
    """`id:` must be the session log's cursor, not a per-connection counter.

    A per-connection counter restarts at 1 on every reconnect, so it means nothing to
    the next connection — which is the entire point of sending one.
    """
    from felix_api.routes.chat import _stream_cursor
    from felix_api.threads import effective_thread_id

    settings = _settings()
    assert await _stream_cursor(settings, "default", None) is None
    await _seed(settings, "one", "two", "three")
    thread = effective_thread_id("default", THREAD)
    assert await _stream_cursor(settings, "default", thread) == 3
