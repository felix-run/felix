"""`Idempotency-Key` on `POST /chat/stream`: one turn per key, however often a client resends.

A streamed send that fails in the client -- a dropped connection, a proxy's 5xx -- may or may
not have reached the turn, and the client cannot tell. felix-web restores such a message and
resends it; without a key on this path that was a second user message and a second model turn.
The app is the production one over real HTTP, and the model is the scripted provider, so the
turn count is the spy's and the user messages are the thread's own transcript.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from felix_ai.providers.scripted import ScriptedClient, ScriptedTurn

from tests.e2e.conftest import Booted

KEY = "idempotency-key"


def _answer(text: str = "noted") -> ScriptedTurn:
    return ScriptedTurn(content=text)


def _send(thread: str, text: str = "resend me") -> dict[str, Any]:
    return {"manifest": "quick", "thread_id": thread, "messages": [{"role": "user", "content": text}]}


def _frames(body: str) -> list[dict[str, Any]]:
    return [
        json.loads(line[len("data: ") :])
        for line in body.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]


async def _user_messages(app: Booted, thread: str) -> list[str]:
    snap = await app.client.get(f"/chat/sessions/{thread}")
    assert snap.status_code == 200, snap.text
    return [str(e.get("content")) for e in snap.json()["transcript"] if e.get("role") == "user"]


async def test_the_same_key_twice_runs_one_turn_and_replays_it(boot: Any) -> None:
    thread = "e2e-idem-stream-twice"
    async with boot([_answer("first answer")]) as app:
        first = await app.client.post("/chat/stream", json=_send(thread), headers={KEY: "send-1"})
        assert first.status_code == 200, first.text
        assert "idempotent-replayed" not in first.headers
        calls = list(app.spy.calls)

        again = await app.client.post("/chat/stream", json=_send(thread), headers={KEY: "send-1"})

        assert again.status_code == 200, again.text
        assert again.headers.get("idempotent-replayed") == "true"
        assert app.spy.calls == calls, "the resend ran a second model turn"
        assert await _user_messages(app, thread) == ["resend me"]
        replayed = [f["data"] for f in _frames(again.text) if f.get("event") == "session_event"]
        assert [(e["role"], e["content"]) for e in replayed if e["role"] in ("user", "assistant")] == [
            ("user", "resend me"),
            ("assistant", "first answer"),
        ]
        assert again.text.rstrip().endswith("data: [DONE]")


async def test_a_resend_while_the_first_is_streaming_is_refused_not_run(boot: Any, monkeypatch: Any) -> None:
    """The first send is held inside its model call; the resend lands then."""
    gate, entered = asyncio.Event(), asyncio.Event()
    original = ScriptedClient.stream_turn

    async def held(self: ScriptedClient, *args: Any, **kwargs: Any) -> Any:
        entered.set()
        await gate.wait()
        async for item in original(self, *args, **kwargs):
            yield item

    monkeypatch.setattr(ScriptedClient, "stream_turn", held)
    thread = "e2e-idem-stream-inflight"
    async with boot([_answer()]) as app:
        first = asyncio.create_task(
            app.client.post("/chat/stream", json=_send(thread), headers={KEY: "send-2"})
        )
        await asyncio.wait_for(entered.wait(), timeout=5)

        during_task = asyncio.create_task(
            app.client.post("/chat/stream", json=_send(thread), headers={KEY: "send-2"})
        )
        # Bounded, so a resend that runs a turn of its own -- and waits at the same gate --
        # fails this test rather than hanging it.
        answered_while_held, _ = await asyncio.wait({during_task}, timeout=3)
        gate.set()
        during = await asyncio.wait_for(during_task, timeout=10)
        done = await asyncio.wait_for(first, timeout=10)

        assert answered_while_held, "the resend waited on the first send's turn instead of being refused"
        assert (during.status_code, during.json()["detail"]) == (409, "idempotency_in_progress"), during.text
        assert done.status_code == 200, done.text
        assert app.spy.calls.count("stream_turn") == 1
        assert await _user_messages(app, thread) == ["resend me"]


async def test_different_keys_run_two_turns(boot: Any) -> None:
    thread = "e2e-idem-stream-two-keys"
    async with boot([_answer(), _answer()]) as app:
        for key in ("send-a", "send-b"):
            resp = await app.client.post("/chat/stream", json=_send(thread), headers={KEY: key})
            assert resp.status_code == 200, resp.text
            assert "idempotent-replayed" not in resp.headers
        assert app.spy.calls.count("stream_turn") == 2
        assert await _user_messages(app, thread) == ["resend me", "resend me"]


async def test_no_key_runs_every_send_as_before(boot: Any) -> None:
    thread = "e2e-idem-stream-no-key"
    async with boot([_answer(), _answer()]) as app:
        for _ in range(2):
            resp = await app.client.post("/chat/stream", json=_send(thread))
            assert resp.status_code == 200, resp.text
        assert app.spy.calls.count("stream_turn") == 2
        assert await _user_messages(app, thread) == ["resend me", "resend me"]


async def test_a_key_reused_with_another_body_or_without_a_thread_is_refused(boot: Any) -> None:
    thread = "e2e-idem-stream-misuse"
    async with boot([_answer()]) as app:
        ok = await app.client.post("/chat/stream", json=_send(thread), headers={KEY: "send-3"})
        assert ok.status_code == 200, ok.text

        other = await app.client.post(
            "/chat/stream", json=_send(thread, "something else"), headers={KEY: "send-3"}
        )
        assert (other.status_code, other.json()["detail"]) == (422, "idempotency_key_reused"), other.text

        threadless = await app.client.post(
            "/chat/stream",
            json={"manifest": "quick", "messages": [{"role": "user", "content": "x"}]},
            headers={KEY: "send-4"},
        )
        assert threadless.status_code == 400, threadless.text
        assert threadless.json()["detail"] == "idempotency_key_requires_thread_id"
        assert app.spy.calls.count("stream_turn") == 1
        assert await _user_messages(app, thread) == ["resend me"]
