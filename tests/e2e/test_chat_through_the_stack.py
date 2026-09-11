"""One request, all the way through — the chain nothing covered.

Each test here boots the zero-argument `create_application()` that Granian is handed in
production, sends real HTTP through it, and lets the compiled agent call a scripted model.
Nothing below the route is replaced, so a control that stops being applied fails here rather
than in an operator's deployment.

The scripted turns are deliberately mechanical (`2+2`), because what is under test is the
path, not the arithmetic.
"""

from __future__ import annotations

import json
from typing import Any

from felix.manifests.loader import parse_manifest
from felix_ai.providers.scripted import ScriptedTurn
from felix_ai.types import TokenUsage, ToolCall

from tests.e2e.conftest import WIRE_MODEL

# Assertions live inside the `async with` throughout: the HTTP client is only usable there,
# and the process globals the audit and usage checks read are reset on the way out.
EMAIL = "alice@example.com"
PII = f"Reach me at {EMAIL} any time."

CALC = ToolCall(id="call-1", name="calculator", args={"expression": "2+2"})


def _tool_turn() -> ScriptedTurn:
    return ScriptedTurn(content="", tool_calls=[CALC], stop_reason="tool_use", usage=TURN_USAGE)


# Stated here rather than inherited from `ScriptedTurn`'s default, so that changing the shared
# double does not redden a metering test that has nothing to say about it.
TURN_USAGE = TokenUsage(input=11, output=7)


def _answer(text: str = "The answer is 4") -> ScriptedTurn:
    return ScriptedTurn(content=text, usage=TURN_USAGE)


def _manifest(name: str, **spec: Any) -> Any:
    """A minimal governed manifest. Anonymous is allowed because the schema default is not,
    and these tests are about what happens *after* the door — a caller with no scopes at all,
    which is what makes the policy denial below a real refusal rather than a 401."""
    base: dict[str, Any] = {
        "pattern": "react",
        "tools": ["calculator"],
        "auth": {"inbound": {"allow_anonymous": True}},
    }
    base.update(spec)
    return parse_manifest(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": name},
            "spec": base,
        }
    )


def _frames(body: str) -> list[dict[str, Any]]:
    """Parse an SSE body into `{event, data}` payloads, dropping the terminator."""
    out: list[dict[str, Any]] = []
    for line in body.splitlines():
        if line.startswith("data: ") and line != "data: [DONE]":
            out.append(json.loads(line[len("data: ") :]))
    return out


async def _audit(settings: Any) -> list[tuple[str, str, str]]:
    """Flush and read back `(event_type, status, tool)` — the write path, not the buffer.

    The tool name is part of the tuple because `("tool_call", "ok")` alone is satisfied by an
    audit row for any tool at all.
    """
    from felix.audit import store as audit_store
    from felix.flush import flush_all

    await flush_all(settings)
    rows, _ = await audit_store.query(settings, "default", limit=200)
    return [
        (row["event_type"], row.get("status") or "", (row.get("payload_json") or {}).get("tool") or "")
        for row in rows
    ]


# --- the happy path ------------------------------------------------------------------------


async def test_a_tool_call_travels_the_whole_stack(boot: Any) -> None:
    """HTTP → auth → resolve → compile → react → governed calculator → reply.

    The tool message carrying `4` is the load-bearing assertion: it can only be there if the
    compiled tool executed, which means the wrapper stack passed the call through rather than
    a stub answering for it.
    """
    async with boot([_tool_turn(), _answer()]) as app:
        resp = await app.client.post(
            "/chat",
            json={"manifest": "quick", "messages": [{"role": "user", "content": "What is 2+2?"}]},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["final"]["content"] == "The answer is 4"
        tool_messages = [m for m in body["messages"] if m.get("role") == "tool"]
        assert [m["content"] for m in tool_messages] == ["4"], body["messages"]


async def test_the_request_took_the_scripted_route(boot: Any) -> None:
    """The turn went to the provider this test registered, not to a default or a stub.

    Without this, every other assertion here could be satisfied by a model that was never
    consulted — and `_resolve_model` running once per invoke is what makes one client serve
    both steps, so two calls on one client is the shape of a completed tool loop.
    """
    async with boot([_tool_turn(), _answer()]) as app:
        resp = await app.client.post(
            "/chat",
            json={"manifest": "quick", "messages": [{"role": "user", "content": "What is 2+2?"}]},
        )
        assert resp.status_code == 200, resp.text
        assert len(app.spy.clients) == 1, "the model is resolved once per invoke"
        assert app.spy.clients[0].route.model == WIRE_MODEL
        assert app.spy.calls == ["chat", "chat"], app.spy.calls


# --- the governance stack is applied, not merely compiled --------------------------------


async def test_a_policy_denies_the_tool_for_a_caller_without_the_scope(boot: Any) -> None:
    """A scoped policy on a manifest reaching a real caller through a real route.

    `apply_policies` returning `tools` unchanged is the exact shape of a control that is
    silently absent, and the wrapper stack is what this whole file exists to keep honest.
    Anonymous callers hold no scopes, so the rule must refuse.
    """
    policed = _manifest(
        "e2e-policed",
        policies=[
            {
                "id": "calc-scope",
                "description": "Calculator requires tools:calc",
                "required_scopes": ["tools:calc"],
                "tools": ["calculator"],
            }
        ],
    )
    async with boot([_tool_turn(), _answer("done")], manifests={"e2e-policed": policed}) as app:
        resp = await app.client.post(
            "/chat",
            json={
                "manifest": "e2e-policed",
                "messages": [{"role": "user", "content": "What is 2+2?"}],
            },
        )
        assert resp.status_code == 200, resp.text
        tool_messages = [m for m in resp.json()["messages"] if m.get("role") == "tool"]
        assert tool_messages, "the loop must still close the tool call it opened"
        assert "policy denied" in tool_messages[0]["content"]
        assert "4" not in tool_messages[0]["content"], "the calculator must not have run"

        assert ("policy_deny", "denied", "calculator") in await _audit(app.settings)


async def test_the_reply_is_screened_on_the_way_out(boot: Any) -> None:
    """Reply controls wrap the pattern, so PII in a model's answer never reaches the client."""
    screened = _manifest("e2e-screened", guardrails={"providers": ["pii"], "targets": ["output"]})
    async with boot([_answer(PII)], manifests={"e2e-screened": screened}) as app:
        resp = await app.client.post(
            "/chat",
            json={"manifest": "e2e-screened", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 200, resp.text
        content = resp.json()["final"]["content"]
        assert EMAIL not in content
        assert "[REDACTED" in content, "the reply must be redacted, not deleted"

        assert ("guardrails_reply", "redacted", "") in await _audit(app.settings)


# --- what the turn leaves behind -----------------------------------------------------------


async def test_the_turn_is_metered_and_priced(boot: Any) -> None:
    """A turn that reaches a model must land on the usage store with a cost.

    One scripted turn, so the token count is exact rather than a floor: an unmetered path
    would leave the row missing, and a mis-keyed price would leave the cost at zero while
    the row still looked right.
    """
    from felix.flush import flush_all
    from felix.usage import store as usage_store

    async with boot([_answer()]) as app:
        resp = await app.client.post(
            "/chat",
            json={"manifest": "quick", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 200, resp.text

        await flush_all(app.settings)
        rows, _ = await usage_store.query(app.settings, "default", limit=50)

    assert len(rows) == 1, rows
    assert rows[0]["tokens_input"] == 11, "the scripted default, so the count is the turn's"
    assert rows[0]["tokens_output"] == 7
    assert rows[0]["cost_usd"] > 0.0, "a metered turn prices on the wire model"


async def test_the_turn_is_audited_from_input_to_answer(boot: Any) -> None:
    """The audit trail an operator reads back: the prompt, the tool, the answer."""
    async with boot([_tool_turn(), _answer()]) as app:
        resp = await app.client.post(
            "/chat",
            json={"manifest": "quick", "messages": [{"role": "user", "content": "What is 2+2?"}]},
        )
        assert resp.status_code == 200, resp.text

        events = await _audit(app.settings)
        assert ("user_input", "ok", "") in events
        assert ("tool_call", "ok", "calculator") in events
        assert ("final_response", "ok", "") in events


async def test_the_stream_carries_the_frames_a_client_resumes_from(boot: Any) -> None:
    """The SSE contract over the real stack: text, the tool, the terminator.

    `tests/unit/test_sse_resume.py` pins the same shape with the agent stubbed out, and owns the
    `id:` cursor contract. This one proves the frames survive a real compile, and that the tool
    actually ran on the streaming path — `tool_start` is yielded before execution, so its
    presence alone would stay green on a tool that never produced anything.
    """
    async with boot([_tool_turn(), _answer()]) as app:
        resp = await app.client.post(
            "/chat/stream",
            json={
                "manifest": "quick",
                "thread_id": "e2e-stream",
                "messages": [{"role": "user", "content": "What is 2+2?"}],
            },
        )
        assert resp.status_code == 200, resp.text
        frames = _frames(resp.text)
        names = [f.get("event") for f in frames]
        assert "text_delta" in names
        assert "tool_start" in names
        assert names[-1] == "done"
        assert resp.text.rstrip().endswith("data: [DONE]")

        outputs = [(f.get("data") or {}).get("output") for f in frames if f.get("event") == "tool_end"]
        assert "4" in outputs, frames


async def test_the_stream_screens_the_reply_before_a_token_ships(boot: Any) -> None:
    """The reply controls on the surface whose failure mode is tokens already sent.

    `ReplyControlsAgent` holds reply text until the run ends and releases it screened. On
    `invoke` a leak is recoverable; on a stream it is already on the wire, which makes this the
    more important of the two paths and the one nothing covered end to end.
    """
    screened = _manifest("e2e-screened", guardrails={"providers": ["pii"], "targets": ["output"]})
    async with boot([_answer(PII)], manifests={"e2e-screened": screened}) as app:
        resp = await app.client.post(
            "/chat/stream",
            json={
                "manifest": "e2e-screened",
                "thread_id": "e2e-screened-stream",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert resp.status_code == 200, resp.text
        # The whole body: not in a delta, not in the terminal `done` frame, nowhere.
        assert EMAIL not in resp.text
        assert "[REDACTED" in resp.text, "the reply must be redacted, not dropped"

        assert ("guardrails_reply", "redacted", "") in await _audit(app.settings)


# --- the middleware stack is real ----------------------------------------------------------


async def test_api_key_mode_refuses_an_unauthenticated_request(boot: Any) -> None:
    """Booting the production factory means the middleware stack is the production one.

    `auth_mode=none` is what every other test here runs under, which would leave the
    authenticated path unexercised on the one factory operators actually deploy.
    """
    key = "sk-e2e-not-a-secret"
    env = {
        "FELIX_AUTH_MODE": "api_key",
        "FELIX_AUTH_API_KEYS": json.dumps({key: {"tenant_id": "default", "sub": "e2e", "scopes": ["admin"]}}),
    }
    async with boot([_answer()], env=env) as app:
        payload = {"manifest": "quick", "messages": [{"role": "user", "content": "hi"}]}

        anonymous = await app.client.post("/chat", json=payload)
        assert anonymous.status_code == 401, anonymous.text
        assert anonymous.headers.get("x-request-id")

        authenticated = await app.client.post(
            "/chat", json=payload, headers={"Authorization": f"Bearer {key}"}
        )
        assert authenticated.status_code == 200, authenticated.text
        assert authenticated.json()["final"]["content"] == "The answer is 4"
        assert authenticated.headers.get("x-request-id")
