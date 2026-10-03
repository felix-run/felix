"""An agent writes a skill, an operator publishes it, and a later session uses it.

The chain no unit test holds end to end: `spec.skill_authoring` → the builder binding
`create_skill` before the governance stack → a draft row and its bytes in the stores the API
booted with → no catalog loading the draft → a publish (called directly; the routes are a later
change) → a fresh compile's catalog listing the live version → `activate_skill` returning its
body to the model.
"""

from __future__ import annotations

import json
from typing import Any

from felix.manifests.loader import parse_manifest
from felix_ai.providers.scripted import ScriptedTurn
from felix_ai.types import ToolCall

NAME = "invoice-triage"
BODY = (
    "# Invoice triage\n\nUse this when an invoice arrives.\n\n## Steps\n\n"
    "1. Read the vendor and the amount.\n2. Route amounts over 500 to the finance queue.\n"
)


def _manifest() -> Any:
    return parse_manifest(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": "e2e-author"},
            "spec": {
                "pattern": "react",
                "tools": ["list_skills", "activate_skill"],
                "skill_authoring": {"enabled": True},
                "auth": {"inbound": {"allow_anonymous": True}},
            },
        }
    )


async def _chat(app: Any, text: str) -> Any:
    response = await app.client.post(
        "/v1/chat/completions", json={"model": "e2e-author", "messages": [{"role": "user", "content": text}]}
    )
    assert response.status_code == 200, response.text
    return response


def _tool_result(prompt: list[Any]) -> str:
    """The last tool message the model was shown in one call."""
    return next(str(m.content) for m in reversed(prompt) if getattr(m, "role", "") == "tool")


async def test_a_skill_the_agent_drafts_is_used_only_after_it_is_published(boot: Any) -> None:
    from felix.skills import library

    create = {
        "name": NAME,
        "description": "Route incoming invoices.",
        "body": BODY,
        "reason": "did it by hand",
    }
    script = [
        ScriptedTurn(tool_calls=[ToolCall(id="c1", name="create_skill", args=create)]),
        ScriptedTurn(content="saved"),
        ScriptedTurn(tool_calls=[ToolCall(id="c2", name="list_skills", args={})]),
        ScriptedTurn(content="listed"),
        ScriptedTurn(tool_calls=[ToolCall(id="c3", name="list_skills", args={})]),
        ScriptedTurn(tool_calls=[ToolCall(id="c4", name="activate_skill", args={"name": NAME})]),
        ScriptedTurn(content="routed"),
    ]
    async with boot(script, manifests={"e2e-author": _manifest()}) as app:
        await _chat(app, "Remember how to triage invoices.")
        saved = json.loads(_tool_result(app.spy.prompts[1]))
        assert (saved["status"], saved["name"], saved["version"]) == ("draft", NAME, "0.1.0")

        # A second conversation: the draft is in the library and in no catalog.
        await _chat(app, "What skills do you have?")
        listed = json.loads(_tool_result(app.spy.prompts[3]))
        assert NAME not in {s["name"] for s in listed}
        assert NAME not in "\n".join(str(m.content) for m in app.spy.prompts[2])

        await library.publish(app.settings, "default", NAME, "0.1.0", by="ops")

        # A third: the published version is listed, offered in the prompt, and activates.
        await _chat(app, "Triage this invoice: ACME, 900.")
        listed = json.loads(_tool_result(app.spy.prompts[5]))
        entry = next(s for s in listed if s["name"] == NAME)
        assert entry["source"] == "library" and entry["has_body"] is True
        assert f'name="{NAME}"' in "\n".join(str(m.content) for m in app.spy.prompts[4])
        activated = json.loads(_tool_result(app.spy.prompts[6]))
        assert activated["activated"] == NAME
        assert "Route amounts over 500 to the finance queue." in activated["instructions"]
