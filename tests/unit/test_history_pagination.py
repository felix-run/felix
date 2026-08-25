"""`GET /chat/history` returns a bounded window, and can be paged backwards.

The endpoint loaded the whole thread and returned every message, so the response grew
without bound for the life of a thread — and a client had no way to ask for less. That
is unbounded growth rather than a latency figure, which is why it survived every
profile.

Two details decide whether this is usable:

*The window is the newest events, not the oldest.* `get_events(limit=n)` takes the
first n on both arms, which for a transcript is the wrong end — a client asking for
"the last 50" would have received the first 50 of a thousand.

*The cursor is the window boundary, not the first message returned.* The filter drops
event kinds that are not messages, so the oldest event in a window may not appear in
the response. Paging from the first message's `seq` would step over whatever was
filtered and lose it.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.config import Settings
from felix.session.store import get_session_store
from felix.session.types import AppendableEvent
from felix_api.threads import effective_thread_id
from httpx import ASGITransport, AsyncClient


def _settings() -> Settings:
    return Settings(
        allow_insecure=True,
        auth_mode="none",
        environment="development",
        object_store="memory",
        database_url="memory://history",
        redis_url="",
    )


def _client(settings: Settings) -> AsyncClient:
    from felix_api.app import create_app

    app = create_app(settings=settings, plugins=[])
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", timeout=30.0)


async def _seed(settings: Settings, thread: str, count: int) -> None:
    session = get_session_store(settings, tenant_id="default").open(
        effective_thread_id("default", thread) or thread
    )
    await session.append_batch(
        [AppendableEvent(kind="message", role="user", content=f"m{i}") for i in range(count)]
    )


async def _history(client: AsyncClient, thread: str, **params: Any) -> dict[str, Any]:
    resp = await client.get(f"/chat/history/{thread}", params=params)
    assert resp.status_code == 200, (resp.status_code, resp.text)
    return resp.json()


@pytest.mark.asyncio
async def test_a_limit_returns_the_newest_events_not_the_oldest() -> None:
    settings = _settings()
    await _seed(settings, "newest", 40)
    async with _client(settings) as client:
        body = await _history(client, "newest", limit=10)

    contents = [m["content"] for m in body["messages"]]
    assert contents[-1] == "m39", f"the window ended at {contents[-1]}, not the newest event"
    assert "m0" not in contents, "returned the oldest events for a limited request"
    assert len(contents) == 10


@pytest.mark.asyncio
async def test_paging_backwards_reaches_the_start_without_gaps() -> None:
    """Walk a thread with the cursor the response hands back and reassemble it."""
    settings = _settings()
    await _seed(settings, "walk", 25)
    seen: list[str] = []
    async with _client(settings) as client:
        body = await _history(client, "walk", limit=10)
        while True:
            seen = [m["content"] for m in body["messages"]] + seen
            if not body["has_more"]:
                break
            body = await _history(client, "walk", limit=10, before_seq=body["oldest_seq"])

    assert seen == [f"m{i}" for i in range(25)], f"paging produced {len(seen)} of 25 events"


@pytest.mark.asyncio
async def test_has_more_is_false_once_the_thread_fits() -> None:
    settings = _settings()
    await _seed(settings, "short", 5)
    async with _client(settings) as client:
        body = await _history(client, "short", limit=10)
    assert body["has_more"] is False
    assert body["oldest_seq"] == 0
    assert len(body["messages"]) == 5


@pytest.mark.asyncio
async def test_the_response_is_bounded_even_with_no_limit_asked_for() -> None:
    """The cap is what makes the growth bounded; the default is unchanged otherwise."""
    from felix_api.routes.chat import MAX_HISTORY_EVENTS

    settings = _settings()
    await _seed(settings, "capped", 60)
    async with _client(settings) as client:
        body = await _history(client, "capped")

    assert len(body["messages"]) == 60, "a thread below the cap should still return whole"
    assert MAX_HISTORY_EVENTS >= 1000, "the cap should be far above any real thread"


@pytest.mark.asyncio
async def test_a_limit_of_zero_is_rejected_rather_than_returning_everything() -> None:
    settings = _settings()
    await _seed(settings, "zero", 3)
    async with _client(settings) as client:
        resp = await client.get("/chat/history/zero", params={"limit": 0})
    assert resp.status_code == 400, resp.status_code


@pytest.mark.asyncio
async def test_an_empty_thread_pages_cleanly() -> None:
    settings = _settings()
    async with _client(settings) as client:
        body = await _history(client, "empty", limit=10)
    assert body["messages"] == []
    assert body["has_more"] is False


@pytest.mark.asyncio
async def test_the_cursor_survives_a_filtered_event_at_the_window_edge() -> None:
    """The oldest event in a window may be one the filter drops.

    Paging from the first *message*'s seq would step over it; paging from the window
    boundary does not. Seeded so the boundary lands on a kind the response omits.
    """
    settings = _settings()
    thread = effective_thread_id("default", "filtered") or "filtered"
    session = get_session_store(settings, tenant_id="default").open(thread)
    await session.append_batch(
        [
            AppendableEvent(kind="message", role="user", content="oldest"),
            AppendableEvent(kind="tool_call", role="tool", content=""),
            AppendableEvent(kind="message", role="user", content="newest"),
        ]
    )
    async with _client(settings) as client:
        body = await _history(client, "filtered", limit=2)
        assert body["oldest_seq"] == 1, f"cursor should be the window edge, got {body['oldest_seq']}"
        older = await _history(client, "filtered", limit=2, before_seq=body["oldest_seq"])

    assert [m["content"] for m in older["messages"]] == ["oldest"], "the filtered edge lost an event"
