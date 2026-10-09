"""`FelixClient.list_sessions` sends the page it was asked for, and only what it was asked for.

`GET /chat/sessions` pages (`limit`, `cursor`, `next_cursor`), and the client is how a script walks
it: a `cursor` that never reached the query string would hand back page one forever.
"""

from __future__ import annotations

import httpx
import pytest
from felix_client import FelixClient

from tests.unit.test_sdk_interrupts import _bind


def _record(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"sessions": [], "items": [], "next_cursor": None})

    monkeypatch.setattr(httpx, "AsyncClient", _bind(httpx.MockTransport(handler)))
    return seen


@pytest.mark.asyncio
async def test_a_page_request_carries_its_limit_and_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _record(monkeypatch)

    await FelixClient(base_url="http://felix").list_sessions(limit=2, cursor="20:acme:b")

    (request,) = seen
    assert request.url.path == "/chat/sessions"
    assert dict(request.url.params) == {"limit": "2", "cursor": "20:acme:b"}


@pytest.mark.asyncio
async def test_no_arguments_leave_the_page_to_the_harness(monkeypatch: pytest.MonkeyPatch) -> None:
    """The route owns the default; a client default would disagree with it the day either moved."""
    seen = _record(monkeypatch)

    await FelixClient(base_url="http://felix").list_sessions()

    (request,) = seen
    assert dict(request.url.params) == {}
