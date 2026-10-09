"""An agent writes a skill, an operator publishes it, and a later session uses it.

The chain no unit test holds end to end: `spec.skill_authoring` → the builder binding
`create_skill` before the governance stack → a draft row and its bytes in the stores the API
booted with → no catalog loading the draft → the draft in the operator's review queue over HTTP
→ a publish through `/skill-library` → a fresh compile's catalog listing the live version →
`activate_skill` returning its body to the model.
"""

from __future__ import annotations

import json
from typing import Any

from felix.manifests.loader import parse_manifest
from felix.skills.library_keys import ORG_OWNER
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

        # The operator's side, over the wire: the draft waits in the review queue, and a
        # publish through the management API is what lets a later session see it.
        queue = await app.client.get("/skill-library/-/review")
        assert queue.status_code == 200, queue.text
        (waiting,) = queue.json()["items"]
        assert (waiting["name"], waiting["version"], waiting["source"]) == (NAME, "0.1.0", "agent")
        assert waiting["origin_manifest_id"] == "e2e-author" and waiting["live_version"] is None
        preview = await app.client.get(f"/skill-library/{NAME}/versions/0.1.0/preview")
        assert preview.json()["policy_passes"] is True, preview.text
        published = await app.client.post(f"/skill-library/{NAME}/versions/0.1.0/publish")
        assert published.status_code == 200, published.text
        assert published.json()["status"] == "published"
        assert (await app.client.get("/skill-library/-/review")).json()["items"] == []

        # A third: the published version is listed, offered in the prompt, and activates.
        await _chat(app, "Triage this invoice: ACME, 900.")
        listed = json.loads(_tool_result(app.spy.prompts[5]))
        entry = next(s for s in listed if s["name"] == NAME)
        assert entry["source"] == "library" and entry["has_body"] is True
        assert f'name="{NAME}"' in "\n".join(str(m.content) for m in app.spy.prompts[4])
        activated = json.loads(_tool_result(app.spy.prompts[6]))
        assert activated["activated"] == NAME
        assert "Route amounts over 500 to the finance queue." in activated["instructions"]


async def test_publishing_over_http_needs_skills_write(boot: Any) -> None:
    """Under `api_key` auth, where the scope gate is live: a key holding `skills:read` sees the
    draft and cannot publish it; one holding `skills:write` can, and is named on the decision."""
    from felix.skills import library
    from felix.skills.format import serialize_skill_md

    keys = {
        "sk-e2e-reader": {"tenant_id": "default", "sub": "reader", "scopes": ["skills:read"]},
        "sk-e2e-writer": {"tenant_id": "default", "sub": "writer", "scopes": ["skills:write"]},
    }
    env = {"FELIX_AUTH_MODE": "api_key", "FELIX_AUTH_API_KEYS": json.dumps(keys)}
    async with boot([], env=env) as app:
        await library.save_draft(
            app.settings,
            "default",
            files={
                "SKILL.md": serialize_skill_md({"name": NAME, "description": "Route invoices."}, f"\n{BODY}")
            },
            provenance=library.DraftProvenance(
                source="agent", author="e2e-author", origin_manifest_id="e2e-author"
            ),
            owner=ORG_OWNER,
        )
        reader = {"Authorization": "Bearer sk-e2e-reader"}
        writer = {"Authorization": "Bearer sk-e2e-writer"}
        assert len((await app.client.get("/skill-library/-/review", headers=reader)).json()["items"]) == 1

        refused = await app.client.post(f"/skill-library/{NAME}/versions/0.1.0/publish", headers=reader)
        assert refused.status_code == 403, refused.text
        assert (await app.client.get(f"/skill-library/{NAME}", headers=reader)).json()["live_version"] is None

        published = await app.client.post(f"/skill-library/{NAME}/versions/0.1.0/publish", headers=writer)
        assert published.status_code == 200, published.text
        assert published.json()["decided_by"] == "writer"
