"""A non-streaming `/chat` says which approvals its run asked for, and how each ended.

The streaming path sends `approval_required` while the tool blocks. `POST /chat` has no stream,
so a caller used to wait out the rule's TTL and receive a denial with nothing saying an approval
had been requested — no id to look up, no reason. These drive the gate through the real stack
both ways: left to time out, and decided while the request is still blocked, found the way a
caller would find it — `GET /approvals?thread_id=`.
"""

from __future__ import annotations

import asyncio
from typing import Any

from felix.manifests.loader import parse_manifest
from felix_ai.providers.scripted import ScriptedTurn
from felix_ai.types import ToolCall

CALC = ToolCall(id="call-1", name="calculator", args={"expression": "2+2"})


def _gated(ttl_seconds: int) -> Any:
    spec = {
        "pattern": "react",
        "tools": ["calculator"],
        "auth": {"inbound": {"allow_anonymous": True}},
        "approvals": [
            {
                "id": "calc-gate",
                "description": "A person signs off on arithmetic.",
                "tools": ["calculator"],
                "ttl_seconds": ttl_seconds,
                "allow_unattended": True,
            }
        ],
    }
    return parse_manifest(
        {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": "e2e-gated"}, "spec": spec}
    )


def _script() -> list[ScriptedTurn]:
    return [ScriptedTurn(content="", tool_calls=[CALC], stop_reason="tool_use"), ScriptedTurn(content="done")]


async def _chat(app: Any) -> Any:
    return await app.client.post(
        "/chat",
        json={
            "manifest": "e2e-gated",
            "thread_id": "e2e-gate",
            "messages": [{"role": "user", "content": "2+2?"}],
        },
    )


async def test_an_approval_left_to_time_out_is_reported_as_expired(boot: Any) -> None:
    async with boot(_script(), manifests={"e2e-gated": _gated(1)}) as app:
        resp = await _chat(app)
    assert resp.status_code == 200, resp.text
    [approval] = resp.json()["approvals"]
    assert approval["status"] == "expired"
    assert approval["tool_name"] == "calculator" and approval["rule_id"] == "calc-gate"
    assert approval["reason"] == "A person signs off on arithmetic."
    assert approval["approval_id"], "the id a caller would decide with"


async def test_an_approval_decided_while_blocked_is_reported_as_approved(boot: Any) -> None:
    async with boot(_script(), manifests={"e2e-gated": _gated(30)}) as app:
        chat = asyncio.create_task(_chat(app))
        pending: list[dict[str, Any]] = []
        for _ in range(100):  # the gate writes its row a moment after the call starts
            await asyncio.sleep(0.05)
            listing = await app.client.get("/approvals", params={"thread_id": "default:e2e-gate"})
            pending = listing.json().get("approvals") or listing.json().get("items") or []
            if pending:
                break
        assert pending, "a blocked caller can find the approval on its thread"
        decided = await app.client.post(
            f"/approvals/{pending[0]['id']}/decide", json={"decision": "approved"}
        )
        assert decided.status_code == 200, decided.text
        resp = await asyncio.wait_for(chat, timeout=30)

    assert resp.status_code == 200, resp.text
    [approval] = resp.json()["approvals"]
    assert approval["status"] == "approved"
    assert approval["approval_id"] == pending[0]["id"]


async def test_a_run_that_asked_for_nothing_reports_no_approvals(boot: Any) -> None:
    async with boot([ScriptedTurn(content="hello")]) as app:
        resp = await app.client.post(
            "/chat", json={"manifest": "quick", "messages": [{"role": "user", "content": "hi"}]}
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["approvals"] == []
