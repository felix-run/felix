"""An image sent to `/v1/chat/completions`, the way an OpenAI SDK sends one.

`content` was typed `str | None` on that surface, so a multimodal request was a 422 before
anything below the route ran — on the one endpoint whose stated purpose is that an OpenAI SDK
works unchanged. Meanwhile `/chat` accepted the same message and both wires had image encoders.

The assertions are on what reached the *model*, not on the reply: the reply is scripted, so a
test that only checks a 200 would pass with the image discarded anywhere along the way.
"""

from __future__ import annotations

from typing import Any

from felix.manifests.loader import parse_manifest
from felix_ai.providers.scripted import ScriptedTurn

PNG = "data:image/png;base64,iVBORw0KGgo="


def _manifest(name: str, **spec: Any) -> Any:
    base: dict[str, Any] = {
        "pattern": "react",
        "tools": [],
        "auth": {"inbound": {"allow_anonymous": True}},
    }
    base.update(spec)
    return parse_manifest(
        {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": name}, "spec": base}
    )


def _image_message(url: str = PNG) -> dict[str, Any]:
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": "what is in this picture?"},
            {"type": "image_url", "image_url": {"url": url}},
        ],
    }


async def test_an_image_reaches_the_model_through_v1(boot: Any) -> None:
    """The whole chain: `/v1` → the request model → `ChatMessage.model_validate` → the compile
    → react → the model call, with the image still attached at the end of it."""
    plain = _manifest("e2e-vision")
    async with boot([ScriptedTurn(content="a logo")], manifests={"e2e-vision": plain}) as app:
        resp = await app.client.post(
            "/v1/chat/completions",
            json={"model": "e2e-vision", "messages": [_image_message()]},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["choices"][0]["message"]["content"] == "a logo"

        seen = [m for call in app.spy.prompts for m in call]
        blocks = [b for m in seen for b in (getattr(m, "content_blocks", None) or [])]
        assert [b.url for b in blocks if b.url] == [PNG], "the image must survive to the model"
        assert any("what is in this picture?" in (b.text or "") for b in blocks)


async def test_a_plain_string_message_still_works(boot: Any) -> None:
    """The counterpart. `content` accepts two shapes now, and the string one is what every
    existing caller sends — widening the type is exactly how that stops being tested."""
    plain = _manifest("e2e-vision")
    async with boot([ScriptedTurn(content="hi")], manifests={"e2e-vision": plain}) as app:
        resp = await app.client.post(
            "/v1/chat/completions",
            json={"model": "e2e-vision", "messages": [{"role": "user", "content": "hello"}]},
        )
        assert resp.status_code == 200, resp.text
        seen = [m for call in app.spy.prompts for m in call]
        assert any(m.content == "hello" for m in seen)
        assert not [b for m in seen for b in (getattr(m, "content_blocks", None) or [])]


async def test_a_message_with_no_content_is_still_accepted(boot: Any) -> None:
    """`None` and `[]` both mean "no content", and widening the field must not make either a
    422. Asserted on what reached the model, not on the 200 — a status-only assertion would
    also pass if the empty message were dropped, merged into the next one, or rendered as the
    literal `"[]"`."""
    plain = _manifest("e2e-vision")
    async with boot([ScriptedTurn(content="ok")], manifests={"e2e-vision": plain}) as app:
        resp = await app.client.post(
            "/v1/chat/completions",
            json={
                "model": "e2e-vision",
                "messages": [
                    {"role": "user", "content": []},
                    {"role": "user", "content": None},
                    {"role": "user", "content": "hi"},
                ],
            },
        )
        assert resp.status_code == 200, resp.text
        user_turns = [m for call in app.spy.prompts for m in call if m.role == "user"]
        assert [m.content for m in user_turns] == ["", "", "hi"]
        assert not [b for m in user_turns for b in (getattr(m, "content_blocks", None) or [])]


async def test_the_screened_text_is_what_the_model_sees_on_an_image_turn(boot: Any) -> None:
    """The control that reported success and changed nothing.

    Both wires prefer `content_blocks` over `.content`, and inbound screening wrote only
    `.content` — so on a multimodal turn PII redaction ran, audited as applied, and the model
    still received the caller's original text. Nothing in `packages/harness` read
    `content_blocks` at all, which is why no existing test could see it: this is the first
    file to send a governed multimodal turn through the whole stack.
    """
    governed = _manifest("e2e-vision-pii", guardrails={"providers": ["pii"], "targets": ["input"]})
    async with boot([ScriptedTurn(content="a logo")], manifests={"e2e-vision-pii": governed}) as app:
        message = _image_message()
        message["content"][0]["text"] = "mail it to alice@example.com and say what this is"
        resp = await app.client.post(
            "/v1/chat/completions",
            json={"model": "e2e-vision-pii", "messages": [message]},
        )
        assert resp.status_code == 200, resp.text

        seen = [m for call in app.spy.prompts for m in call]
        blocks = [b for m in seen for b in (getattr(m, "content_blocks", None) or [])]
        block_text = " ".join(b.text or "" for b in blocks)
        assert "alice@example.com" not in block_text, "the model was shown what was redacted"
        assert "REDACTED" in block_text, "and the redaction must actually be there"
        assert [b.url for b in blocks if b.url] == [PNG], "the image itself must survive screening"


async def test_an_image_survives_the_session_round_trip(boot: Any) -> None:
    """Turn one and turn two take different branches of both wires.

    `session/types.py` persists and restores `attachments` and not `content_blocks`, so a
    replayed image arrives in the older shape — which is why one image had four renderings and
    why a divergence between them stayed invisible until the second request. One normaliser
    above both wires is what collapses that, and this is the test that would see it come back.

    Two *different* images, and the thread comes from `user` — which is how this surface
    addresses one. With a single image the assertion would be satisfied by the turn being sent
    right now, and an ineffective thread id would look exactly like a working replay.
    """
    first = "data:image/png;base64,iVBORw0KGgoFIRST="
    second = "data:image/png;base64,iVBORw0KGgoSECOND="
    plain = _manifest("e2e-vision")
    async with boot(
        [ScriptedTurn(content="a logo"), ScriptedTurn(content="still a logo")],
        manifests={"e2e-vision": plain},
    ) as app:
        for question, image in (("what is this?", first), ("are you sure?", second)):
            message = _image_message(image)
            message["content"][0]["text"] = question
            resp = await app.client.post(
                "/v1/chat/completions",
                json={"model": "e2e-vision", "messages": [message], "user": "vision-thread"},
            )
            assert resp.status_code == 200, resp.text

        replayed = app.spy.prompts[-1]
        carried = [att.url for m in replayed for att in (getattr(m, "attachments", None) or [])]
        carried += [b.url for m in replayed for b in (getattr(m, "content_blocks", None) or []) if b.url]
        assert second in carried, "the image sent on this turn must be there"
        assert first in carried, "and so must the one replayed out of the session log"
        assert any("what is this?" in (getattr(m, "content", "") or "") for m in replayed), (
            "the first turn must actually have been replayed, or the assertion above is "
            "about a thread that was never continued"
        )


async def test_a_tool_result_keeps_the_id_that_ties_it_to_its_call(boot: Any) -> None:
    """`name` and `tool_call_id` were declared on the `/v1` request model, validated, and then
    thrown away — only `role` and `content` were forwarded. So an SDK doing the standard tool
    round-trip sent a result whose id was dropped on the floor, and the model was handed a
    tool message answering nothing in particular.

    A seam that accepts input and silently discards it is worse than no seam, which is the
    repo's own rule; this is that rule applied to two fields on a surface whose whole promise
    is that an OpenAI SDK works unchanged.
    """
    plain = _manifest("e2e-vision")
    async with boot([ScriptedTurn(content="ok")], manifests={"e2e-vision": plain}) as app:
        resp = await app.client.post(
            "/v1/chat/completions",
            json={
                "model": "e2e-vision",
                "messages": [
                    {"role": "user", "content": "what is 2+2?"},
                    {"role": "tool", "content": "4", "tool_call_id": "call-1", "name": "calculator"},
                ],
            },
        )
        assert resp.status_code == 200, resp.text
        seen = [m for call in app.spy.prompts for m in call]
        tool_messages = [m for m in seen if m.role == "tool"]
        assert [m.tool_call_id for m in tool_messages] == ["call-1"]
        assert [m.name for m in tool_messages] == ["calculator"]
