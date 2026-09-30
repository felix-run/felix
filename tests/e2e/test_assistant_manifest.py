"""The bundled `assistant` manifest: memory on, the way a deployment runs it.

It is the manifest the README points at for an assistant that remembers, so what is under test
is the promise made there — a fact stored in one session reaches the next session's prompt, the
memory tools are offered, and an anonymous caller is refused, because memory is shared across a
tenant and an anonymous caller could read another's facts.
"""

from __future__ import annotations

import json
from typing import Any

from felix_ai.providers.scripted import ScriptedTurn
from felix_ai.types import ToolCall

KEY = "sk-e2e-not-a-secret"
ENV = {
    "FELIX_AUTH_MODE": "api_key",
    "FELIX_AUTH_API_KEYS": json.dumps({KEY: {"tenant_id": "default", "sub": "e2e", "scopes": ["admin"]}}),
}
AUTH = {"Authorization": f"Bearer {KEY}"}
FACT = "The user's timezone is Europe/Lisbon."
REMEMBER = ToolCall(
    id="call-1", name="remember", args={"content": FACT, "topic_key": "user.timezone", "importance": 0.8}
)


def _chat(thread: str, text: str) -> dict[str, Any]:
    return {"manifest": "assistant", "thread_id": thread, "messages": [{"role": "user", "content": text}]}


async def test_a_fact_remembered_in_one_session_reaches_the_next(boot: Any) -> None:
    # Short turns on purpose: under `capture.min_chars` no extraction call runs, so the script
    # is exactly the turns below and the fact comes from the tool, not from capture.
    script = [
        ScriptedTurn(content="", tool_calls=[REMEMBER], stop_reason="tool_use"),
        ScriptedTurn(content="Noted."),
        ScriptedTurn(content="Lisbon time."),
    ]
    async with boot(script, env=ENV) as app:
        first = await app.client.post("/chat", json=_chat("t-one", "I live in Lisbon."), headers=AUTH)
        assert first.status_code == 200, first.text
        offered = set(app.spy.tools[0])
        assert {"remember", "recall", "forget", "list_memories"} <= offered, offered

        second = await app.client.post("/chat", json=_chat("t-two", "What time zone am I in?"), headers=AUTH)
        assert second.status_code == 200, second.text
        [*_, last] = app.spy.prompts
        assert any(FACT in str(getattr(m, "content", "")) for m in last), (
            "the fact stored in t-one was not in t-two's prompt"
        )


async def test_an_anonymous_caller_is_refused_even_without_auth(boot: Any) -> None:
    """`make dev` runs `FELIX_AUTH_MODE=none`; the README says `assistant` answers 401 there."""
    async with boot([], env={"FELIX_AUTH_MODE": "none"}) as app:
        resp = await app.client.post("/chat", json=_chat("t-anon", "hi"))
        assert resp.status_code == 401, resp.text
        assert app.spy.calls == []
