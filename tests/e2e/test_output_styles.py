"""Output styles through the stack: `POST /chat/style`, then the model's system prompt."""

from __future__ import annotations

from typing import Any

from felix.manifests.loader import parse_manifest
from felix_ai.providers.scripted import ScriptedTurn

from tests.support.e2e import without_date

THREAD = "styled-thread"


def _agent() -> Any:
    spec = {
        "pattern": "react",
        "auth": {"inbound": {"allow_anonymous": True}},
        "system_prompt": {"base": "You are the styled agent."},
        "output_styles": {
            "default": "terse",
            "styles": [
                {"name": "terse", "description": "Short answers", "prompt": "Answer in one sentence."},
                {"name": "teach", "prompt": "Explain each step as you go."},
            ],
        },
    }
    return parse_manifest(
        {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": "e2e-styled"}, "spec": spec}
    )


async def _turn(app: Any) -> str:
    resp = await app.client.post(
        "/chat",
        json={"manifest": "e2e-styled", "thread_id": THREAD, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200, resp.text
    [system] = [m.content for m in app.spy.prompts[-1] if m.role == "system"]
    return without_date(system)


async def _set(app: Any, style: str | None) -> Any:
    return await app.client.post(
        "/chat/style", json={"thread_id": THREAD, "manifest": "e2e-styled", "style": style}
    )


async def test_a_chosen_style_joins_the_system_prompt_and_null_restores_the_default(boot: Any) -> None:
    turns = [ScriptedTurn(content="ok")] * 3
    async with boot(turns, manifests={"e2e-styled": _agent()}) as app:
        first = await _turn(app)
        assert (await _set(app, "Teach")).json()["style"] == "teach", "stored as declared"
        second = await _turn(app)
        snapshot = (await app.client.get(f"/chat/sessions/{THREAD}")).json()
        assert (await _set(app, None)).status_code == 200
        third = await _turn(app)
    assert first.startswith("You are the styled agent.")
    assert first.endswith("# Output style: terse\n\nAnswer in one sentence.")
    assert second.endswith("# Output style: teach\n\nExplain each step as you go.")
    assert "Answer in one sentence." not in second
    assert snapshot["outputStyle"] == "teach"
    assert third == first


async def test_a_style_the_manifest_does_not_declare_is_refused_and_not_stored(boot: Any) -> None:
    async with boot([ScriptedTurn(content="ok")], manifests={"e2e-styled": _agent()}) as app:
        assert (await _set(app, "teach")).status_code == 200
        resp = await _set(app, "pirate")
        assert resp.status_code == 422
        assert resp.json()["detail"] == "unknown_output_style"
        assert (await app.client.get(f"/chat/sessions/{THREAD}")).json()["outputStyle"] == "teach"
        assert (await _turn(app)).endswith("Explain each step as you go.")


async def test_a_named_style_needs_a_manifest_that_resolves(boot: Any) -> None:
    async with boot(manifests={"e2e-styled": _agent()}) as app:
        missing = await app.client.post("/chat/style", json={"thread_id": THREAD, "style": "teach"})
        unknown = await app.client.post(
            "/chat/style", json={"thread_id": THREAD, "manifest": "nope", "style": "teach"}
        )
        cleared = await app.client.post("/chat/style", json={"thread_id": THREAD, "style": None})
    assert (missing.status_code, missing.json()["detail"]) == (422, "manifest_required")
    assert (unknown.status_code, unknown.json()["detail"]) == (404, "unknown_manifest")
    assert cleared.status_code == 200 and cleared.json()["style"] is None


async def test_the_model_listing_carries_the_styles(boot: Any) -> None:
    async with boot(manifests={"e2e-styled": _agent()}) as app:
        listing = (await app.client.get("/v1/models")).json()
    [entry] = [m for m in listing["data"] if m["id"] == "e2e-styled"]
    assert entry["felix"]["outputStyles"] == [
        {"name": "terse", "description": "Short answers"},
        {"name": "teach", "description": None},
    ]
    assert entry["felix"]["defaultOutputStyle"] == "terse"
