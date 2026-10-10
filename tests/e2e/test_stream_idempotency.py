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

from tests.support.e2e import Booted

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


# --- review fixes: an early disconnect, other appends, a failed turn, a racing claim ----------


def _raw_scope(body: bytes, key: str) -> dict[str, Any]:
    """An ASGI 2.3 request, as Granian sends one: Starlette then watches for `http.disconnect`."""
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/chat/stream",
        "raw_path": b"/chat/stream",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"felix.test"),
            (b"content-type", b"application/json"),
            (b"idempotency-key", key.encode()),
            (b"content-length", str(len(body)).encode()),
        ],
        "client": ("127.0.0.1", 1),
        "server": ("felix.test", 80),
    }


async def test_a_disconnect_before_the_first_byte_frees_the_key(boot: Any) -> None:
    """The client is gone while `http.response.start` is still being sent.

    Starlette cancels the response before the body generator is ever iterated, so a settle in
    the generator's `finally` never ran, and the key sat `in_progress` for its whole TTL: every
    resend was `409` for a day. Nothing was appended, so the resend must run the turn.
    """
    thread = "e2e-idem-stream-early-disconnect"
    async with boot([_answer("after the resend")]) as app:
        asgi = app.client._transport.app  # type: ignore[attr-defined]
        body = json.dumps(_send(thread)).encode()
        inbound = [{"type": "http.request", "body": body, "more_body": False}]

        async def receive() -> dict[str, Any]:
            if inbound:
                return inbound.pop(0)
            await asyncio.sleep(0.05)
            return {"type": "http.disconnect"}

        sent: list[str] = []

        async def send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                await asyncio.sleep(1)  # the disconnect lands first
            sent.append(message["type"])

        await asyncio.wait_for(asgi(_raw_scope(body, "send-early"), receive, send), timeout=10)
        assert "http.response.body" not in sent, sent
        assert app.spy.calls.count("stream_turn") == 0

        resend = await app.client.post("/chat/stream", json=_send(thread), headers={KEY: "send-early"})

        assert resend.status_code == 200, resend.text
        assert "idempotent-replayed" not in resend.headers
        assert app.spy.calls.count("stream_turn") == 1
        assert await _user_messages(app, thread) == ["resend me"]


async def test_a_disconnect_mid_turn_keeps_the_key_and_replays_the_user_message(
    boot: Any, monkeypatch: Any
) -> None:
    """The client drops while the model is answering: the turn is torn down, the message stays.

    The resend must not send the message a second time; it replays it, and runs nothing.
    """
    gate, entered = asyncio.Event(), asyncio.Event()
    original = ScriptedClient.stream_turn
    model_calls: list[str] = []

    async def held(self: ScriptedClient, *args: Any, **kwargs: Any) -> Any:
        model_calls.append("stream_turn")
        entered.set()
        await gate.wait()
        async for item in original(self, *args, **kwargs):
            yield item

    monkeypatch.setattr(ScriptedClient, "stream_turn", held)
    thread = "e2e-idem-stream-mid-disconnect"
    async with boot([_answer()]) as app:
        asgi = app.client._transport.app  # type: ignore[attr-defined]
        body = json.dumps(_send(thread)).encode()
        inbound = [{"type": "http.request", "body": body, "more_body": False}]

        async def receive() -> dict[str, Any]:
            if inbound:
                return inbound.pop(0)
            await entered.wait()
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            return None

        await asyncio.wait_for(asgi(_raw_scope(body, "send-mid"), receive, send), timeout=10)
        gate.set()

        resend = await app.client.post("/chat/stream", json=_send(thread), headers={KEY: "send-mid"})

        assert resend.status_code == 200, resend.text
        assert resend.headers.get("idempotent-replayed") == "true"
        replayed = [f["data"] for f in _frames(resend.text) if f.get("event") == "session_event"]
        assert [(e["role"], e["content"]) for e in replayed] == [("user", "resend me")]
        assert model_calls == ["stream_turn"], "the resend called the model again"
        assert await _user_messages(app, thread) == ["resend me"]


async def test_the_replay_carries_only_this_requests_events(boot: Any, monkeypatch: Any) -> None:
    """Another request appends to the thread while the first streams; its entry is not replayed."""
    gate, entered = asyncio.Event(), asyncio.Event()
    original = ScriptedClient.stream_turn

    async def held(self: ScriptedClient, *args: Any, **kwargs: Any) -> Any:
        entered.set()
        await gate.wait()
        async for item in original(self, *args, **kwargs):
            yield item

    monkeypatch.setattr(ScriptedClient, "stream_turn", held)
    thread = "e2e-idem-stream-interleaved"
    async with boot([_answer("mine")]) as app:
        first = asyncio.create_task(
            app.client.post("/chat/stream", json=_send(thread), headers={KEY: "send-5"})
        )
        await asyncio.wait_for(entered.wait(), timeout=5)
        other = await app.client.post(
            "/chat/sessions/custom", json={"thread_id": thread, "content": "not this request's"}
        )
        assert other.status_code == 200, other.text
        gate.set()
        assert (await asyncio.wait_for(first, timeout=10)).status_code == 200

        again = await app.client.post("/chat/stream", json=_send(thread), headers={KEY: "send-5"})

        assert again.headers.get("idempotent-replayed") == "true"
        replayed = [
            (f["data"]["role"], f["data"]["content"])
            for f in _frames(again.text)
            if f.get("event") == "session_event"
        ]
        assert ("system", "not this request's") not in replayed, replayed
        assert [r for r in replayed if r[0] in ("user", "assistant")] == [
            ("user", "resend me"),
            ("assistant", "mine"),
        ]


async def test_a_failed_turn_replays_its_user_message_and_its_error(boot: Any) -> None:
    thread = "e2e-idem-stream-failed"
    async with boot([]) as app:
        first = await app.client.post("/chat/stream", json=_send(thread), headers={KEY: "send-6"})
        assert first.status_code == 200, first.text
        assert "event: error" in first.text, first.text

        again = await app.client.post("/chat/stream", json=_send(thread), headers={KEY: "send-6"})

        assert again.headers.get("idempotent-replayed") == "true"
        replayed = [f["data"] for f in _frames(again.text) if f.get("event") == "session_event"]
        assert [e["content"] for e in replayed if e["role"] == "user"] == ["resend me"]
        assert "event: error" in again.text, again.text
        first_error = first.text.split("event: error\n", 1)[1].split("\n", 1)[0]
        assert first_error in again.text
        assert again.text.rstrip().endswith("data: [DONE]")


async def test_a_claim_that_raises_a_conflict_is_a_409_not_a_500(boot: Any, monkeypatch: Any) -> None:
    """The Redis store raises `IdempotencyConflict` when the key keeps changing under it."""
    from felix.idempotency import IdempotencyConflict

    async def contended(*_a: Any, **_k: Any) -> Any:
        raise IdempotencyConflict("in_progress")

    thread = "e2e-idem-stream-conflict"
    async with boot([]) as app:
        monkeypatch.setattr(app.client._transport.app.state.idempotency_store, "claim", contended)  # type: ignore[attr-defined]
        resp = await app.client.post("/chat/stream", json=_send(thread), headers={KEY: "send-7"})
        assert (resp.status_code, resp.json()["detail"]) == (409, "idempotency_in_progress"), resp.text
        assert app.spy.calls == []


async def test_one_subject_at_two_issuers_does_not_share_a_streamed_key(boot: Any, monkeypatch: Any) -> None:
    """`alice` at one identity provider and `alice` at another are two callers with two personal
    skill libraries; a streamed resend of one's key is not the other's turn to replay."""
    from felix.auth.context import AuthContext, Principal
    from felix.plugins import PluginRegistry

    async def no_token(_target: Any) -> str:
        return ""

    def builder(settings: Any) -> Any:
        async def authenticate(request: Any) -> AuthContext:
            principal = Principal(
                subject="alice",
                tenant_id="default",
                scopes=frozenset({"*"}),
                issuer=request.headers["x-test-issuer"],
                scheme="test",
            )
            return AuthContext(principal=principal, outbound_token=no_token, anonymous=False)

        return authenticate

    registry = PluginRegistry()
    registry.register_authenticator("two-issuers", builder)
    monkeypatch.setattr("felix.plugins._registry", registry)
    thread = "e2e-idem-stream-issuers"
    async with boot(
        [_answer("a's answer"), _answer("b's answer")], env={"FELIX_AUTH_MODE": "two-issuers"}
    ) as app:
        replies = [
            await app.client.post(
                "/chat/stream", json=_send(thread), headers={KEY: "send-1", "x-test-issuer": issuer}
            )
            for issuer in ("https://a.example", "https://b.example", "https://a.example")
        ]
        turns = len(app.spy.calls)
    assert [r.status_code for r in replies] == [200, 200, 200], [r.text[:200] for r in replies]
    assert "idempotent-replayed" not in replies[1].headers, "b.example's alice was replayed a.example's turn"
    assert replies[2].headers.get("idempotent-replayed") == "true"
    assert turns == 2
