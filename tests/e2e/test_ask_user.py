"""`ask_user`: the model asks the person watching the run, and the answer comes back to it.

`request_ui` existed and both web clients render its frame, but no tool exposed it, so no
agent could ever ask. These drive the tool through the real stack: a scripted model calls it,
the question goes out on the stream, `POST /chat/ui` answers it, and the answer is the tool's
result. And where nobody is watching -- a non-streaming `/chat` -- it must refuse at once
rather than block the run for the whole timeout.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from typing import Any

import pytest
from felix.manifests.loader import parse_manifest
from felix_ai.providers.scripted import ScriptedTurn
from felix_ai.types import ToolCall

REQUEST_ID = "e2e-ask-1"
ASK = ToolCall(
    id="call-ask",
    name="ask_user",
    args={"question": "Deploy to staging or production?", "options": ["staging", "production"]},
)


def _manifest() -> Any:
    spec = {"pattern": "react", "tools": ["ask_user"], "auth": {"inbound": {"allow_anonymous": True}}}
    return parse_manifest(
        {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": "e2e-ask"}, "spec": spec}
    )


def _script() -> list[ScriptedTurn]:
    return [
        ScriptedTurn(content="", tool_calls=[ASK], stop_reason="tool_use"),
        ScriptedTurn(content="Deploying to staging."),
    ]


def _frames(body: str) -> list[dict[str, Any]]:
    return [
        json.loads(line[len("data: ") :])
        for line in body.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]


@pytest.fixture
def fixed_request_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the prompt's id so the test can answer it -- in `felix.ui.prompts` only, since
    patching the global `secrets` would hand every lease and token the same value."""
    from felix.ui import prompts

    monkeypatch.setattr(prompts, "secrets", SimpleNamespace(token_urlsafe=lambda _n: REQUEST_ID))


async def test_the_question_goes_out_on_the_stream_and_the_answer_comes_back(
    boot: Any, fixed_request_id: None
) -> None:
    async with boot(_script(), manifests={"e2e-ask": _manifest()}) as app:
        # Answered before the run asks: a signal that arrives first is held for the waiter,
        # which is what lets a test that reads whole responses answer a blocking prompt.
        answered = await app.client.post(
            "/chat/ui", json={"thread_id": "e2e-ask", "request_id": REQUEST_ID, "value": "staging"}
        )
        assert answered.status_code == 200, answered.text

        resp = await app.client.post(
            "/chat/stream",
            json={
                "manifest": "e2e-ask",
                "thread_id": "e2e-ask",
                "messages": [{"role": "user", "content": "ship it"}],
            },
        )
        assert resp.status_code == 200, resp.text
        frames = _frames(resp.text)

        asked = [f["data"] for f in frames if f.get("event") == "ui_request"]
        assert asked and asked[0]["kind"] == "select", frames
        assert asked[0]["prompt"] == "Deploy to staging or production?"
        assert asked[0]["options"] == ["staging", "production"]

        outputs = [(f.get("data") or {}).get("output") for f in frames if f.get("event") == "tool_end"]
        assert json.loads(outputs[0]) == {"answered": True, "value": "staging"}, outputs


async def test_with_no_one_watching_it_refuses_at_once(boot: Any) -> None:
    """A non-streaming `/chat` has no consumer for the side channel, so a question would never
    reach anyone. The tool says so immediately instead of holding the run for its timeout."""
    async with boot(_script(), manifests={"e2e-ask": _manifest()}) as app:
        started = time.monotonic()
        resp = await app.client.post(
            "/chat",
            json={
                "manifest": "e2e-ask",
                "thread_id": "e2e-ask-unwatched",
                "messages": [{"role": "user", "content": "ship it"}],
            },
        )
        elapsed = time.monotonic() - started
        assert resp.status_code == 200, resp.text
        assert elapsed < 5, f"the run waited {elapsed:.1f}s for an answer nobody could give"

        tool_results = [m["content"] for m in resp.json().get("messages", []) if m.get("role") == "tool"]
        assert tool_results, resp.json()
        result = json.loads(tool_results[0])
        assert result["answered"] is False and result["reason"] == "no_one_watching", result
