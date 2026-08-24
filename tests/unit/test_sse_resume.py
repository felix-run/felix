"""Recovering a chat stream after it drops.

A client that loses `POST /chat/stream` mid-turn had nothing to come back to: no `id:`
on any frame, and no route to reconnect to. The run itself is still torn down on
disconnect — deliberately, so a hung-up client stops burning tokens — so what a
reconnect recovers is the thread as it now stands, not the abandoned turn.

Every `id:` on both streams means one thing: the next session sequence to ask for. A
client hands it straight back as `Last-Event-ID`.

One limitation worth stating plainly: `ASGITransport` buffers, so nothing here
observes *incremental* delivery. These tests pin what a stream contains, not that a
client sees frames as they are produced — proving that needs a real server.
"""

from __future__ import annotations

import json

import pytest
from felix.config import Settings
from felix.patterns.types import Event
from felix.session.store import get_session_store
from felix.session.types import AppendableEvent
from felix_api.threads import effective_thread_id
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def thread(request: pytest.FixtureRequest) -> str:
    """A thread name nobody else touches.

    The in-memory session store is a process global that outlives a test, so a shared
    name leaks state into whatever module runs next. A unique name per test is cheaper
    and safer than reaching into the store's privates to clear it.
    """
    return f"resume-{request.node.name}"


def _settings() -> Settings:
    return Settings(
        allow_insecure=True,
        auth_mode="none",
        environment="development",
        object_store="memory",
        database_url="memory://resume",
        host="127.0.0.1",
        # No Redis here; the snapshot path consults it for lease and steer state and
        # would otherwise spend the test retrying a refused port.
        redis_url="",
        # Runtime is bounded by this, not by the read loop below — see `_read`.
        stream_resume_idle_seconds=0.4,
        stream_resume_poll_seconds=0.1,
    )


def _client(settings: Settings) -> AsyncClient:
    from felix_api.app import create_app

    app = create_app(settings=settings, plugins=[])
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", timeout=30.0)


async def _seed(settings: Settings, thread: str, *texts: str) -> None:
    """Append events to the thread id the route will actually open.

    Threads are namespaced per tenant on the way in, so seeding the bare name would
    write somewhere the route never looks.
    """
    namespaced = effective_thread_id("default", thread)
    assert namespaced is not None
    session = get_session_store(settings, tenant_id="default").open(namespaced)
    for text in texts:
        await session.append(AppendableEvent(kind="message", role="user", content=text))


def _frames(body: str) -> list[dict]:
    """Parse an SSE body into id/name/payload triples.

    Keeps the `event:` name rather than discarding it, so an error frame is
    distinguishable instead of blowing up downstream on a missing key.
    """
    out: list[dict] = []
    for block in body.split("\n\n"):
        if not block.strip() or block.lstrip().startswith(":"):
            continue
        event_id, name, data = None, None, None
        for line in block.splitlines():
            if line.startswith("id: "):
                event_id = int(line[4:])
            elif line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = line[6:]
        if data is None:
            continue
        out.append(
            {
                "id": event_id,
                "name": name,
                "done": data == "[DONE]",
                "payload": None if data == "[DONE]" else json.loads(data),
            }
        )
    return out


def _data_frames(body: str) -> list[dict]:
    return [f for f in _frames(body) if not f["done"] and f["name"] is None]


async def _read(client: AsyncClient, url: str, *, headers: dict | None = None) -> str:
    """Collect a stream body.

    `ASGITransport` buffers: headers do not arrive until the generator finishes and
    the whole body lands as one chunk. So runtime here is exactly
    `stream_resume_idle_seconds`, which is why `_settings` turns it right down rather
    than leaving the production five minutes.
    """
    body = ""
    async with client.stream("GET", url, headers=headers or {}) as resp:
        assert resp.status_code == 200, resp.status_code
        assert resp.headers["content-type"].startswith("text/event-stream")
        # A proxied stream that omits this is buffered by nginx and delivers nothing
        # until it closes, which would defeat the entire feature.
        assert resp.headers.get("x-accel-buffering") == "no"
        async for chunk in resp.aiter_text():
            body += chunk
    return body


# --- reconnecting ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cold_reconnect_returns_the_transcript(thread: str) -> None:
    """No cursor means the client has nothing, so hand back the thread itself.

    Asserts the transcript, not just the frame's label: an implementation that sent an
    empty snapshot would satisfy the label and fail the client completely.
    """
    settings = _settings()
    await _seed(settings, thread, "first", "second")

    async with _client(settings) as client:
        body = await _read(client, f"/chat/stream/{thread}")

    frames = _data_frames(body)
    assert len(frames) == 1, "the tail re-sent what the snapshot already carried"
    assert frames[0]["payload"]["event"] == "snapshot"
    assert frames[0]["id"] == 2, "the cursor must be the next sequence, not the last one"

    transcript = frames[0]["payload"]["data"]["transcript"]
    assert [m["content"] for m in transcript] == ["first", "second"]


@pytest.mark.asyncio
async def test_warm_reconnect_replays_only_what_was_missed(thread: str) -> None:
    """The point of `Last-Event-ID`: do not re-send what the client already has."""
    settings = _settings()
    await _seed(settings, thread, "one", "two", "three")

    async with _client(settings) as client:
        body = await _read(client, f"/chat/stream/{thread}", headers={"Last-Event-ID": "2"})

    frames = _data_frames(body)
    assert frames, "nothing was replayed"
    assert not any(f["payload"]["event"] == "snapshot" for f in frames), (
        "a warm reconnect resent the whole transcript"
    )
    assert [f["payload"]["data"]["content"] for f in frames] == ["three"]


@pytest.mark.asyncio
async def test_the_cursor_a_reconnect_returns_is_usable_again(thread: str) -> None:
    """Round trip: the id from one connection resumes correctly on the next."""
    settings = _settings()
    await _seed(settings, thread, "one", "two")

    async with _client(settings) as client:
        cursor = _data_frames(await _read(client, f"/chat/stream/{thread}"))[0]["id"]
        await _seed(settings, thread, "three")
        body = await _read(client, f"/chat/stream/{thread}", headers={"Last-Event-ID": str(cursor)})

    assert [f["payload"]["data"]["content"] for f in _data_frames(body)] == ["three"]


@pytest.mark.asyncio
async def test_last_event_id_also_accepted_as_a_query_parameter(thread: str) -> None:
    """EventSource sets the header; a plain fetch or curl cannot."""
    settings = _settings()
    await _seed(settings, thread, "one", "two")

    async with _client(settings) as client:
        frames = _data_frames(await _read(client, f"/chat/stream/{thread}?last_event_id=1"))

    assert not any(f["payload"]["event"] == "snapshot" for f in frames)
    assert [f["payload"]["data"]["content"] for f in frames] == ["two"]


@pytest.mark.asyncio
async def test_a_garbage_cursor_degrades_to_a_snapshot(thread: str) -> None:
    """A malformed `Last-Event-ID` must not 500 a recovery surface."""
    settings = _settings()
    await _seed(settings, thread, "one")

    async with _client(settings) as client:
        body = await _read(client, f"/chat/stream/{thread}", headers={"Last-Event-ID": "not-a-number"})

    assert _data_frames(body)[0]["payload"]["event"] == "snapshot"


@pytest.mark.asyncio
async def test_the_stream_terminates_cleanly(thread: str) -> None:
    """Without `[DONE]` the body just stops under an already-sent 200."""
    settings = _settings()
    await _seed(settings, thread, "one")

    async with _client(settings) as client:
        body = await _read(client, f"/chat/stream/{thread}")

    assert _frames(body)[-1]["done"], "the stream ended without [DONE]"


@pytest.mark.asyncio
async def test_an_unknown_thread_returns_an_empty_snapshot(thread: str) -> None:
    """Deliberately not a 404: that would answer whether someone else's thread exists."""
    async with _client(_settings()) as client:
        frames = _data_frames(await _read(client, f"/chat/stream/{thread}-never-existed"))

    assert frames[0]["payload"]["event"] == "snapshot"
    assert frames[0]["id"] == 0
    assert frames[0]["payload"]["data"]["transcript"] == []


@pytest.mark.asyncio
async def test_a_thread_id_carrying_a_tenant_delimiter_is_rejected(thread: str) -> None:
    """`:` is how the tenant prefix is encoded, so accepting one invites forgery.

    The previous version of this test asserted `in (400, 404)` against a traversal
    string, which Starlette's router rejects before the route runs — so it never
    reached the branch it claimed to cover.
    """
    async with _client(_settings()) as client:
        resp = await client.get("/chat/stream/other%3Athread")

    assert resp.status_code == 400
    assert resp.json()["detail"] == "invalid_thread_id"


@pytest.mark.asyncio
async def test_a_reconnect_cannot_read_another_tenants_thread(thread: str) -> None:
    """Thread ids are namespaced per tenant; the route must use the namespaced one."""
    settings = _settings()
    namespaced = effective_thread_id("acme", thread)
    assert namespaced is not None
    session = get_session_store(settings, tenant_id="acme").open(namespaced)
    await session.append(AppendableEvent(kind="message", role="user", content="acme secret"))

    # auth_mode=none pins the caller to the `default` tenant.
    async with _client(settings) as client:
        body = await _read(client, f"/chat/stream/{thread}")

    assert "acme secret" not in body
    assert _data_frames(body)[0]["payload"]["data"]["transcript"] == []


# --- the cursor on the outbound stream ---------------------------------------------


class _ScriptedAgent:
    """Yields a fixed frame sequence, so the route's stamping is what is under test."""

    async def stream_events(self, _input):
        yield Event(event="text_delta", data={"delta": "He"})
        yield Event(event="tool_start", data={"tool": "calculator"})
        yield Event(event="text_delta", data={"delta": "llo"})
        yield Event(event="done", data={})


@pytest.mark.asyncio
async def test_post_stream_stamps_structural_frames_only(
    thread: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The half of the feature that makes reconnecting possible at all.

    Without a correct `id:` on the outbound stream there is no `Last-Event-ID` to come
    back with. Nothing covered this before: inverting the stamping condition left the
    whole suite green, and replacing the allowlist with a denylist — a real change in
    which frames carry a cursor — was noticed only by a test that restated the
    constant.
    """
    import felix_api.routes.chat as chat_mod

    settings = _settings()

    async def _fake_build(*_a, **_kw):
        return _ScriptedAgent()

    monkeypatch.setattr(chat_mod, "build_tenant_agent", _fake_build)

    async with _client(settings) as client:
        resp = await client.post(
            "/chat/stream",
            json={
                "manifest": "quick",
                "thread_id": thread,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    assert resp.status_code == 200

    stamped = {f["payload"]["event"]: f["id"] for f in _data_frames(resp.text)}
    assert stamped["text_delta"] is None, "per-token frames must not cost a cursor query"
    assert stamped["tool_start"] is not None, "a structural frame must be resumable"
    assert stamped["done"] is not None


# --- which frames are resume points ------------------------------------------------


def test_per_token_frames_are_not_resume_points() -> None:
    """Frames without an `id:` leave `lastEventId` untouched, which is what we want."""
    from felix_api.routes._sse import is_resume_point

    for name in ("text_delta", "on_chat_model_stream", "session_progress"):
        assert not is_resume_point(name), f"{name} arrives per token and must not be stamped"
    assert not is_resume_point(""), "an unnamed frame has nothing to resume from"


def test_pause_frames_are_resume_points() -> None:
    """Why this is a denylist and not an allowlist.

    An approval or client-tool request is the longest a stream goes quiet, and so the
    likeliest moment for a connection to drop — exactly what resuming is for. An
    allowlist had missed every one of them. Patterns also register through an open
    registry, so core cannot enumerate the event vocabulary, and anything it failed to
    list would be silently unresumable.
    """
    from felix_api.routes._sse import is_resume_point

    for name in (
        "approval_required",
        "tool_request",
        "ui_request",
        "steer",
        "follow_up",
        "done",
        "tool_end",
        "an_event_from_a_plugin_registered_pattern",
    ):
        assert is_resume_point(name), f"{name} should carry a resume cursor"


@pytest.mark.asyncio
async def test_stream_cursor_is_the_next_sequence(thread: str) -> None:
    """A per-connection counter restarts at 1 and so means nothing to the next one."""
    from felix_api.routes.chat import _stream_cursor

    settings = _settings()
    assert await _stream_cursor(settings, "default", None) is None
    await _seed(settings, thread, "one", "two", "three")
    assert await _stream_cursor(settings, "default", effective_thread_id("default", thread)) == 3
