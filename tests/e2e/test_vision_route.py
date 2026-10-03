"""An image sent to an agent whose model cannot see one.

`quick` on a deployment whose default route was a text-only Workers AI model answered every
picture with "I can't see images": the bytes reached a model that ignored them, and the reply
read as the client's fault. These run the reported shape through the real app -- a manifest
naming no model, the default route vouched text-only -- and assert on which client the image
reached, because the reply is scripted and would read the same either way.
"""

from __future__ import annotations

import json
from typing import Any

from felix.manifests.loader import parse_manifest
from felix_ai.providers.scripted import ScriptedTurn

from tests.e2e.conftest import DEFAULT_ROUTE, WIRE_MODEL, _scripted_routes

PNG = "data:image/png;base64,iVBORw0KGgo="


def _routes_with_text_only_default() -> str:
    routes: dict[str, dict[str, Any]] = dict(_scripted_routes())
    routes[DEFAULT_ROUTE] = {"provider": "scripted", "model": WIRE_MODEL, "modalities": ["text"]}
    routes["e2e-vision"] = {"provider": "scripted", "model": WIRE_MODEL, "modalities": ["text", "image"]}
    return json.dumps(routes)


def _manifest(name: str, **model: Any) -> Any:
    spec: dict[str, Any] = {"pattern": "react", "tools": [], "auth": {"inbound": {"allow_anonymous": True}}}
    if model:
        spec["model"] = model
    return parse_manifest(
        {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": name}, "spec": spec}
    )


def _image_turn() -> dict[str, Any]:
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": "what is in this picture?"},
            {"type": "image_url", "image_url": {"url": PNG}},
        ],
    }


async def test_v1_refuses_an_image_no_route_can_see(boot: Any) -> None:
    env = {"FELIX_MODEL_ROUTES": _routes_with_text_only_default(), "FELIX_DEFAULT_VISION_MODEL_ID": ""}
    async with boot(
        [ScriptedTurn(content="unseen")], env=env, manifests={"blind": _manifest("blind")}
    ) as app:
        resp = await app.client.post(
            "/v1/chat/completions", json={"model": "blind", "messages": [_image_turn()]}
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["error"]["code"] == "model_not_vision_capable"
        assert DEFAULT_ROUTE in resp.json()["error"]["message"]
        assert app.spy.calls == [], "a refused turn must not reach any model"


async def test_chat_refuses_an_image_no_route_can_see(boot: Any) -> None:
    env = {"FELIX_MODEL_ROUTES": _routes_with_text_only_default(), "FELIX_DEFAULT_VISION_MODEL_ID": ""}
    async with boot(
        [ScriptedTurn(content="unseen")], env=env, manifests={"blind": _manifest("blind")}
    ) as app:
        resp = await app.client.post("/chat", json={"manifest": "blind", "messages": [_image_turn()]})
        assert resp.status_code == 422, resp.text
        assert DEFAULT_ROUTE in resp.json()["detail"]
        assert app.spy.calls == []


async def test_a_text_turn_to_a_text_only_route_is_untouched(boot: Any) -> None:
    env = {"FELIX_MODEL_ROUTES": _routes_with_text_only_default(), "FELIX_DEFAULT_VISION_MODEL_ID": ""}
    async with boot([ScriptedTurn(content="hi")], env=env, manifests={"blind": _manifest("blind")}) as app:
        resp = await app.client.post(
            "/v1/chat/completions",
            json={"model": "blind", "messages": [{"role": "user", "content": "hello"}]},
        )
        assert resp.status_code == 200, resp.text
        assert [c.model_id for c in app.spy.clients if c.calls] == [DEFAULT_ROUTE]
        assert [m.content for call in app.spy.prompts for m in call if m.role == "user"] == ["hello"]


async def test_the_default_vision_route_answers_the_image(boot: Any) -> None:
    env = {
        "FELIX_MODEL_ROUTES": _routes_with_text_only_default(),
        "FELIX_DEFAULT_VISION_MODEL_ID": "e2e-vision",
    }
    async with boot([ScriptedTurn(content="a logo")], env=env, manifests={"seer": _manifest("seer")}) as app:
        resp = await app.client.post(
            "/v1/chat/completions", json={"model": "seer", "messages": [_image_turn()]}
        )
        assert resp.status_code == 200, resp.text

        answered = [c for c in app.spy.clients if c.calls]
        assert [c.model_id for c in answered] == ["e2e-vision"], "the image must go to the vision route"
        seen = [b.url for call in app.spy.prompts for m in call for b in (m.content_blocks or []) if b.url]
        assert seen == [PNG], "and arrive with the image still attached"


async def test_the_manifest_vision_model_beats_the_default(boot: Any) -> None:
    routes = json.loads(_routes_with_text_only_default())
    routes["e2e-vision-2"] = {**routes["e2e-vision"]}
    env = {"FELIX_MODEL_ROUTES": json.dumps(routes), "FELIX_DEFAULT_VISION_MODEL_ID": "e2e-vision"}
    seer = _manifest("seer", vision_model="e2e-vision-2")
    async with boot([ScriptedTurn(content="a logo")], env=env, manifests={"seer": seer}) as app:
        resp = await app.client.post(
            "/v1/chat/completions", json={"model": "seer", "messages": [_image_turn()]}
        )
        assert resp.status_code == 200, resp.text
        assert [c.model_id for c in app.spy.clients if c.calls] == ["e2e-vision-2"]


async def test_chat_stream_refuses_before_the_stream_opens(boot: Any) -> None:
    """The route the check exists for: past this point a raise holds the connection open."""
    env = {"FELIX_MODEL_ROUTES": _routes_with_text_only_default(), "FELIX_DEFAULT_VISION_MODEL_ID": ""}
    async with boot(
        [ScriptedTurn(content="unseen")], env=env, manifests={"blind": _manifest("blind")}
    ) as app:
        resp = await app.client.post("/chat/stream", json={"manifest": "blind", "messages": [_image_turn()]})
        assert resp.status_code == 422, resp.text
        assert DEFAULT_ROUTE in resp.json()["detail"]
        assert app.spy.calls == []


async def test_the_manifest_vision_model_alone_satisfies_the_check(boot: Any) -> None:
    """No deployment default: only the manifest's own route can let the turn through."""
    env = {"FELIX_MODEL_ROUTES": _routes_with_text_only_default(), "FELIX_DEFAULT_VISION_MODEL_ID": ""}
    seer = _manifest("seer", vision_model="e2e-vision")
    async with boot([ScriptedTurn(content="a logo")], env=env, manifests={"seer": seer}) as app:
        resp = await app.client.post("/chat", json={"manifest": "seer", "messages": [_image_turn()]})
        assert resp.status_code == 200, resp.text
        assert [c.model_id for c in app.spy.clients if c.calls] == ["e2e-vision"]
