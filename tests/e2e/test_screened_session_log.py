"""The session log keeps the reply the client got, not the one the model wrote.

The react loop appends each assistant message to the thread's log as the run goes, before the
reply controls see the output, so screening only what leaves the run left the raw reply in the
export, the history and every replay. These read the thread back over HTTP after a turn, which
is the only place the difference shows: the response itself was already screened.
"""

from __future__ import annotations

import json
from typing import Any

from felix_ai.providers.scripted import ScriptedTurn
from felix_ai.types import ToolCall

from tests.support.screening import DECIDER_ENV, EMAIL, PII, audit_rows, governed_manifest, judged_manifest

PII_GUARDRAILS = {"providers": ["pii"], "targets": ["output"]}


async def _export(app: Any, thread: str) -> list[dict[str, Any]]:
    resp = await app.client.get(f"/chat/sessions/{thread}/export")
    assert resp.status_code == 200, resp.text
    return [json.loads(line) for line in resp.text.splitlines() if line.strip()]


def _assistant_text(events: list[dict[str, Any]]) -> list[str]:
    return [str(e.get("content") or "") for e in events if e.get("role") == "assistant"]


async def test_a_redacted_reply_is_redacted_in_the_log(boot: Any) -> None:
    screened = governed_manifest("e2e-screened", guardrails=PII_GUARDRAILS)
    async with boot([ScriptedTurn(content=PII)], manifests={"e2e-screened": screened}) as app:
        resp = await app.client.post(
            "/chat",
            json={
                "manifest": "e2e-screened",
                "thread_id": "logged",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert resp.status_code == 200, resp.text
        events = await _export(app, "logged")
        history = (await app.client.get("/chat/history/logged")).text
        audit = await audit_rows(app.settings)

    [reply] = _assistant_text(events)
    assert EMAIL not in reply and "[REDACTED" in reply
    assert reply == resp.json()["final"]["content"], "the log keeps what the client got"
    assert EMAIL not in history
    assert audit.count(("guardrails_reply", "redacted", "")) == 1, "one verdict, not one per write"


async def test_a_streamed_reply_is_redacted_in_the_log(boot: Any) -> None:
    screened = governed_manifest("e2e-screened", guardrails=PII_GUARDRAILS)
    async with boot([ScriptedTurn(content=PII)], manifests={"e2e-screened": screened}) as app:
        resp = await app.client.post(
            "/chat/stream",
            json={
                "manifest": "e2e-screened",
                "thread_id": "streamed",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert resp.status_code == 200, resp.text
        events = await _export(app, "streamed")
    assert _assistant_text(events) and all(EMAIL not in t for t in _assistant_text(events))


async def test_a_preamble_before_tool_calls_is_redacted_in_the_log(boot: Any) -> None:
    """Every assistant message is written, not only the answer, so every one is screened."""
    call = ToolCall(id="call-1", name="calculator", args={"expression": "2+2"})
    script = [
        ScriptedTurn(content=PII, tool_calls=[call], stop_reason="tool_use"),
        ScriptedTurn(content="4"),
    ]
    screened = governed_manifest("e2e-screened", guardrails=PII_GUARDRAILS)
    async with boot(script, manifests={"e2e-screened": screened}) as app:
        resp = await app.client.post(
            "/chat",
            json={
                "manifest": "e2e-screened",
                "thread_id": "preamble",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert resp.status_code == 200, resp.text
        events = await _export(app, "preamble")
    texts = _assistant_text(events)
    assert len(texts) == 2 and all(EMAIL not in t for t in texts)


async def test_a_denied_reply_is_stored_as_its_denial(boot: Any, verdict: Any) -> None:
    m = judged_manifest(final_response=True)
    async with boot(
        [ScriptedTurn(content="Let me tell you about cats.")], env=DECIDER_ENV, manifests={"e2e-judged": m}
    ) as app:
        resp = await app.client.post(
            "/chat",
            json={
                "manifest": "e2e-judged",
                "thread_id": "denied",
                "messages": [{"role": "user", "content": "What is 2+2?"}],
            },
        )
        assert resp.status_code == 200, resp.text
        events = await _export(app, "denied")

    [reply] = _assistant_text(events)
    assert reply.startswith("[judge denied] on-topic") and "cats" not in reply
    assert verdict["judged"] == ["Let me tell you about cats."], "judged once, for the log and the reply"


async def test_a_routers_child_logs_through_the_routers_screen(boot: Any) -> None:
    """The router forwards the caller's turn, thread and all, so its child writes the caller's
    log. An unguarded child compiled against the raw store logged the reply the router's own
    controls redacted on the wire."""
    from felix.manifests.loader import parse_manifest

    def agent(name: str, **spec: Any) -> Any:
        base = {"pattern": "react", "auth": {"inbound": {"allow_anonymous": True}}, **spec}
        return parse_manifest(
            {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": name}, "spec": base}
        )

    manifests = {
        "e2e-plain": agent("e2e-plain"),
        "e2e-router": agent(
            "e2e-router", pattern="router", sub_agents=["e2e-plain"], guardrails=PII_GUARDRAILS
        ),
    }
    script = [ScriptedTurn(content="e2e-plain"), ScriptedTurn(content=PII)]
    async with boot(script, manifests=manifests) as app:
        resp = await app.client.post(
            "/chat",
            json={
                "manifest": "e2e-router",
                "thread_id": "routed",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert resp.status_code == 200, resp.text
        events = await _export(app, "routed")
    texts = _assistant_text(events)
    assert texts and all(EMAIL not in t for t in texts), texts


async def test_reflect_quotes_its_draft_redacted(boot: Any, verdict: Any) -> None:
    """Reflect hands the draft back as a user turn, which the log keeps and the assistant-only
    screen would not reach."""
    from felix.manifests.loader import parse_manifest

    spec = {
        "pattern": "reflect",
        "auth": {"inbound": {"allow_anonymous": True}},
        "decider": {"id": "e2e-decider"},
        "reflect": {"decider": True, "max_iterations": 2, "threshold": 0.5},
        "guardrails": PII_GUARDRAILS,
    }
    m = parse_manifest(
        {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": "e2e-reflect"}, "spec": spec}
    )
    script = [ScriptedTurn(content=PII), ScriptedTurn(content="A better answer.")]
    async with boot(script, env=DECIDER_ENV, manifests={"e2e-reflect": m}) as app:
        resp = await app.client.post(
            "/chat",
            json={
                "manifest": "e2e-reflect",
                "thread_id": "reflected",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert resp.status_code == 200, resp.text
        raw = (await app.client.get("/chat/sessions/reflected/export")).text
    assert "Prior answer" in raw, "the critique turn is in the log"
    assert EMAIL not in raw


# --- memory capture ------------------------------------------------------------------------

CAPTURE = {"enabled": True, "min_chars": 0}


async def test_memory_is_captured_from_the_redacted_reply(boot: Any) -> None:
    """Capture runs inside the reply controls, on the pattern's own output: extracting from it
    stored a fact the controls redacted, and recall put it back into every later prompt."""
    screened = governed_manifest("e2e-screened", guardrails=PII_GUARDRAILS, memory={"capture": CAPTURE})
    async with boot(
        [ScriptedTurn(content=PII), ScriptedTurn(content="[]")], manifests={"e2e-screened": screened}
    ) as app:
        resp = await app.client.post(
            "/chat",
            json={"manifest": "e2e-screened", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 200, resp.text
        prompts = app.spy.prompts
    assert len(prompts) == 2, "the reply, then the extraction"
    extraction = " ".join(str(m.content) for m in prompts[1])
    assert "[REDACTED" in extraction and EMAIL not in extraction


async def test_nothing_is_captured_from_a_denied_reply(boot: Any, verdict: Any) -> None:
    from felix.manifests.loader import parse_manifest

    base = judged_manifest(final_response=True)
    spec = {**base.spec.model_dump(exclude_defaults=True, mode="json"), "memory": {"capture": CAPTURE}}
    m = parse_manifest(
        {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": "e2e-judged"}, "spec": spec}
    )
    async with boot(
        [ScriptedTurn(content="Let me tell you about cats."), ScriptedTurn(content="[]")],
        env=DECIDER_ENV,
        manifests={"e2e-judged": m},
    ) as app:
        resp = await app.client.post(
            "/v1/chat/completions",
            json={"model": "e2e-judged", "messages": [{"role": "user", "content": "What is 2+2?"}]},
        )
        assert resp.status_code == 200, resp.text
        prompts = app.spy.prompts
    assert len(prompts) == 1, "a denied reply is not an answer to learn from"


async def test_a_routers_child_captures_through_the_routers_screen(boot: Any) -> None:
    """The child's own manifest has no reply controls; the router's still govern its answer."""
    from felix.manifests.loader import parse_manifest

    def agent(name: str, **spec: Any) -> Any:
        base = {"pattern": "react", "auth": {"inbound": {"allow_anonymous": True}}, **spec}
        return parse_manifest(
            {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": name}, "spec": base}
        )

    manifests = {
        "e2e-plain": agent("e2e-plain", memory={"capture": CAPTURE}),
        "e2e-router": agent(
            "e2e-router", pattern="router", sub_agents=["e2e-plain"], guardrails=PII_GUARDRAILS
        ),
    }
    script = [ScriptedTurn(content="e2e-plain"), ScriptedTurn(content=PII), ScriptedTurn(content="[]")]
    async with boot(script, manifests=manifests) as app:
        resp = await app.client.post(
            "/chat", json={"manifest": "e2e-router", "messages": [{"role": "user", "content": "hi"}]}
        )
        assert resp.status_code == 200, resp.text
        prompts = app.spy.prompts
    assert len(prompts) == 3, "classify, answer, extract"
    extraction = " ".join(str(m.content) for m in prompts[2])
    assert "[REDACTED" in extraction and EMAIL not in extraction


async def test_a_child_with_its_own_controls_still_owes_its_routers(boot: Any, verdict: Any) -> None:
    """A child whose manifest has a judge of its own screens with that — and with the router's
    PII redaction above it, which the child's screen chains to rather than replaces."""
    from felix.manifests.loader import parse_manifest

    verdict["p"] = 0.9  # the child's judge accepts

    def agent(name: str, **spec: Any) -> Any:
        base = {"pattern": "react", "auth": {"inbound": {"allow_anonymous": True}}, **spec}
        return parse_manifest(
            {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": name}, "spec": base}
        )

    judge = {"name": "fine", "criteria": "is fine", "threshold": 0.5, "decider": True, "final_response": True}
    manifests = {
        "e2e-judged-child": agent(
            "e2e-judged-child",
            decider={"id": "e2e-decider"},
            guardrails={"judges": [judge]},
            memory={"capture": CAPTURE},
        ),
        "e2e-router": agent(
            "e2e-router", pattern="router", sub_agents=["e2e-judged-child"], guardrails=PII_GUARDRAILS
        ),
    }
    script = [ScriptedTurn(content="e2e-judged-child"), ScriptedTurn(content=PII), ScriptedTurn(content="[]")]
    async with boot(script, env=DECIDER_ENV, manifests=manifests) as app:
        resp = await app.client.post(
            "/chat", json={"manifest": "e2e-router", "messages": [{"role": "user", "content": "hi"}]}
        )
        assert resp.status_code == 200, resp.text
        prompts = app.spy.prompts
    assert verdict["judged"], "the child's own judge ran"
    extraction = " ".join(str(m.content) for m in prompts[-1])
    assert "[REDACTED" in extraction and EMAIL not in extraction
