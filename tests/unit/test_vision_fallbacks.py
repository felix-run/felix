"""A vision route that fails over, and a fallback that is metered as the route that answered.

The vision route was built alone: no fallbacks, so one provider error on an image turn failed
the turn while the same manifest's text turns had a chain to fall back on. And a fallback that
answered -- on either chain -- was metered as the primary, priced at a model it never reached.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from felix.config import Settings
from felix.patterns.model import build_model, record_model_usage
from felix.patterns.model_registry import register_model_provider
from felix_ai.providers.scripted import ScriptedClient, ScriptedTurn
from felix_ai.types import ChatMessage, ImageAttachment, ModelChatResult
from felix_ai.wire import ModelGatewayError

PNG = "data:image/png;base64,iVBORw0KGgo="
TEXT_ONLY = {"provider": "scripted", "model": "@cf/openai/gpt-oss-120b", "modalities": ["text"]}
SEEING = {"provider": "scripted", "model": "claude-sonnet-5", "modalities": ["text", "image"]}
SEEING_TOO = {"provider": "scripted", "model": "claude-haiku-4-5", "modalities": ["text", "image"]}


def _settings(**routes: dict[str, Any]) -> Settings:
    return Settings(model_routes=json.dumps(routes), default_model_id="cheap", default_vision_model_id="")


def _down() -> ScriptedTurn:
    return ScriptedTurn(error=ModelGatewayError("scripted", 503, ""))


@pytest.fixture
def scripted() -> Any:
    """One script per route, so a test says what each model answers."""
    from felix_ai import registry

    scripts: dict[str, list[ScriptedTurn]] = {}
    built: dict[str, ScriptedClient] = {}

    def factory(model_id: str, route: Any, spec: Any, settings: Any) -> ScriptedClient:
        client = ScriptedClient(model_id=model_id, route=route, script=scripts.setdefault(model_id, []))
        built[model_id] = client
        return client

    saved = dict(registry._providers)
    register_model_provider("scripted", factory)
    yield SimpleNamespace(scripts=scripts, built=built)
    registry._providers.clear()
    registry._providers.update(saved)


def _looking() -> list[ChatMessage]:
    return [ChatMessage(role="user", content="look", attachments=[ImageAttachment(url=PNG)])]


async def _drain(model: Any, how: str, messages: list[ChatMessage]) -> ModelChatResult:
    if how == "chat":
        return await model.chat(messages, [])
    result = None
    async for item in model.stream_turn(messages, []):
        if isinstance(item, ModelChatResult):
            result = item
    assert result is not None
    return result


@pytest.mark.parametrize("how", ["chat", "stream_turn"])
async def test_a_failing_vision_route_falls_over_to_a_fallback_that_can_see(scripted: Any, how: str) -> None:
    settings = _settings(cheap=TEXT_ONLY, seer=SEEING, cheap2=TEXT_ONLY, seer2=SEEING_TOO)
    scripted.scripts.update(seer=[_down()], seer2=[ScriptedTurn(content="a logo")])
    model = build_model(
        settings, SimpleNamespace(id=None, vision_model="seer", fallbacks=["cheap2", "seer2"])
    )

    result = await _drain(model, how, _looking())
    assert result.message.content == "a logo"
    assert result.served_model_id == "seer2", "metered as the route that answered"
    assert scripted.built["cheap2"].calls == [], "a text-only fallback is never handed an image"


async def test_with_no_fallback_that_can_see_the_vision_error_stands(scripted: Any) -> None:
    settings = _settings(cheap=TEXT_ONLY, seer=SEEING, cheap2=TEXT_ONLY)
    scripted.scripts.update(seer=[_down()])
    model = build_model(settings, SimpleNamespace(id=None, vision_model="seer", fallbacks=["cheap2"]))
    with pytest.raises(ModelGatewayError):
        await model.chat(_looking(), [])


async def test_a_text_turn_still_uses_the_primary_chain(scripted: Any) -> None:
    settings = _settings(cheap=TEXT_ONLY, seer=SEEING, cheap2=TEXT_ONLY)
    scripted.scripts.update(cheap=[_down()], cheap2=[ScriptedTurn(content="4")])
    model = build_model(settings, SimpleNamespace(id=None, vision_model="seer", fallbacks=["cheap2"]))
    result = await model.chat([ChatMessage(role="user", content="2+2?")], [])
    assert (result.message.content, result.served_model_id) == ("4", "cheap2")
    assert "seer" not in scripted.built or scripted.built["seer"].calls == []


@pytest.mark.parametrize("how", ["chat", "stream_turn"])
async def test_a_primary_chain_fallback_is_metered_as_itself(
    scripted: Any, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    """The fallback's own wire id is priced, not the primary's it stood in for."""
    metered: list[tuple[str | None, str | None]] = []
    monkeypatch.setattr(
        "felix.patterns.model.record_usage",
        lambda result, **kw: metered.append((kw["model_id"], kw["wire_model_id"])) or {},
    )
    settings = _settings(cheap=TEXT_ONLY, cheap2=SEEING)
    scripted.scripts.update(cheap=[_down()], cheap2=[ScriptedTurn(content="4")])
    model = build_model(settings, SimpleNamespace(id=None, fallbacks=["cheap2"]))

    record_model_usage(
        await _drain(model, how, [ChatMessage(role="user", content="2+2?")]), model, manifest_id="m"
    )
    assert metered == [("cheap2", "claude-sonnet-5")]
