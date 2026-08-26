"""`FelixClient` can answer both interrupts that hold a run open, not just one.

`tool_request` and `approval_required` each park the model loop until something
replies. The client could answer a `ui_request` and neither of these, so a headless
caller — the one that most needs to, because nobody is watching it park — received
the frame and had no method to respond. The run then sat until its lease expired.

That the browser client implements both is what made it easy to miss: the capability
existed, just not on the path the harness is meant to be driven from.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from felix.sdk import FelixClient


def _bind(transport: httpx.MockTransport) -> type[httpx.AsyncClient]:
    real = httpx.AsyncClient

    class _Bound(real):  # type: ignore[misc,valid-type]
        def __init__(self, *a: Any, **k: Any) -> None:
            k["transport"] = transport
            super().__init__(*a, **k)

    return _Bound


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch):
    """A Felix that echoes back what it was asked, so the request can be asserted on."""
    seen: list[httpx.Request] = []

    def start(payload: dict[str, Any] | None = None, status_code: int = 200):
        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(status_code, json=payload if payload is not None else {"ok": True})

        monkeypatch.setattr(httpx, "AsyncClient", _bind(httpx.MockTransport(handler)))
        return seen

    return start


@pytest.mark.asyncio
async def test_tool_result_posts_the_call_id_and_thread(server) -> None:
    seen = server({"ok": True})
    client = FelixClient(base_url="http://felix")
    client.set_thread("t1")

    await client.tool_result("call-7", {"files": ["a.txt"]})

    (request,) = seen
    assert request.url.path == "/chat/tool_result"

    body = json.loads(request.content)
    assert body == {
        "thread_id": "t1",
        "tool_call_id": "call-7",
        "content": {"files": ["a.txt"]},
        "error": False,
    }


@pytest.mark.asyncio
async def test_tool_result_reports_a_failure_rather_than_staying_silent(server) -> None:
    # A failed client tool still has to answer: the run cannot tell a tool that
    # errored from one that was never run, and waits either way.
    seen = server({"ok": True})
    client = FelixClient(base_url="http://felix")

    await client.tool_result("call-7", "ENOENT", thread_id="t1", error=True)

    body = json.loads(seen[0].content)
    assert body["error"] is True
    assert body["content"] == "ENOENT"


@pytest.mark.asyncio
async def test_tool_result_refuses_without_a_thread(server) -> None:
    server({"ok": True})
    client = FelixClient(base_url="http://felix")
    with pytest.raises(ValueError, match="thread_id required"):
        await client.tool_result("call-7", "x")


@pytest.mark.asyncio
async def test_decide_approval_sends_the_wire_spelling_not_the_bool(server) -> None:
    seen = server({"status": "approved"})
    client = FelixClient(base_url="http://felix")

    await client.decide_approval("appr-1", approved=True)

    (request,) = seen
    assert request.url.path == "/approvals/appr-1/decide"
    assert json.loads(request.content) == {"decision": "approved", "note": ""}


@pytest.mark.asyncio
async def test_decide_approval_denies_with_a_note(server) -> None:
    seen = server({"status": "denied"})
    client = FelixClient(base_url="http://felix")

    await client.decide_approval("appr-1", approved=False, note="writes outside the workspace")

    body = json.loads(seen[0].content)
    assert body["decision"] == "denied"
    assert body["note"] == "writes outside the workspace"


@pytest.mark.asyncio
async def test_decide_approval_carries_edited_args_only_when_given(server) -> None:
    # Approving a *modified* call is the third option, and the reason the decision
    # has a body at all. Omitted rather than null when absent, so an untouched
    # approval cannot be mistaken for one edited to nothing.
    seen = server({"status": "approved"})
    client = FelixClient(base_url="http://felix")

    await client.decide_approval("appr-1", approved=True, edited_args={"path": "safe.txt"})
    await client.decide_approval("appr-2", approved=True)

    assert json.loads(seen[0].content)["edited_args"] == {"path": "safe.txt"}
    assert "edited_args" not in json.loads(seen[1].content)


@pytest.mark.asyncio
async def test_list_approvals_defaults_to_pending(server) -> None:
    seen = server({"items": [], "requests": []})
    client = FelixClient(base_url="http://felix")

    await client.list_approvals()

    (request,) = seen
    assert request.url.path == "/approvals"
    assert request.url.params["status"] == "pending"
    assert request.url.params["limit"] == "50"


@pytest.mark.asyncio
async def test_list_approvals_can_ask_for_every_status(server) -> None:
    # `status=None` means "do not filter"; sending the literal string "None" would
    # match nothing and read as an empty queue.
    seen = server({"items": []})
    client = FelixClient(base_url="http://felix")

    await client.list_approvals(status=None, limit=10)

    assert "status" not in seen[0].url.params
    assert seen[0].url.params["limit"] == "10"
