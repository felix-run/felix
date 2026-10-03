"""Which routes can see an image, and what happens to one sent where it cannot be seen."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from felix.config import Settings
from felix.patterns.model import build_model, parse_model_routes
from felix.patterns.model_registry import register_model_provider
from felix.patterns.model_vision import (
    _VisionRoutingClient,
    image_route_problem,
    route_accepts_images,
    without_images,
)
from felix_ai.catalog import accepts_images
from felix_ai.providers.scripted import ScriptedClient, ScriptedTurn
from felix_ai.types import ChatMessage, ContentBlock, ImageAttachment, ModelRoute

PNG = "data:image/png;base64,iVBORw0KGgo="


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("claude-sonnet-5", True),
        ("@cf/moonshotai/kimi-k2.6", True),
        ("@cf/openai/gpt-oss-120b", False),
        ("@cf/zai-org/glm-4.7-flash", False),
        # The `llama` family key matches this and says nothing about images; stripping them
        # from a vision model on the strength of a substring would be the bug, inverted.
        ("llama3.2-vision", None),
        ("some-model-felix-has-never-heard-of", None),
    ],
)
def test_the_catalog_answers_only_what_it_can_vouch_for(model: str, expected: bool | None) -> None:
    assert accepts_images(model) is expected


def test_a_route_declaration_beats_the_catalog() -> None:
    assert route_accepts_images(ModelRoute("ollama", "llava", modalities=("text", "image"))) is True
    assert route_accepts_images(ModelRoute("anthropic", "claude-sonnet-5", modalities=("text",))) is False


def test_routes_read_modalities_from_the_setting() -> None:
    raw = json.dumps({"seer": {"provider": "ollama", "model": "llava", "modalities": ["text", "image"]}})
    routes = parse_model_routes(Settings(model_routes=raw))
    assert routes["seer"].modalities == ("text", "image")
    assert routes["claude-sonnet"].modalities is None


def test_without_images_names_the_route_for_each_image() -> None:
    attached = ChatMessage(role="user", content="look", attachments=[ImageAttachment(url=PNG)] * 2)
    blocks = ChatMessage(
        role="user",
        content="look",
        content_blocks=[ContentBlock(type="text", text="look"), ContentBlock(type="image_url", url=PNG)],
    )
    out = without_images([attached, blocks], "cheap")
    assert out[0].attachments is None
    assert out[0].content.count("route 'cheap' does not accept images") == 2
    assert [b.type for b in out[1].content_blocks or []] == ["text", "text"]
    assert all(not b.url for b in out[1].content_blocks or [])


def test_without_images_leaves_a_text_conversation_alone() -> None:
    messages = [ChatMessage(role="user", content="hi")]
    assert without_images(messages, "cheap") is messages


def _settings(**routes: dict[str, Any]) -> Settings:
    return Settings(model_routes=json.dumps(routes), default_model_id="cheap")


TEXT_ONLY = {"provider": "scripted", "model": "claude-sonnet-5", "modalities": ["text"]}
SEEING = {"provider": "scripted", "model": "claude-sonnet-5", "modalities": ["text", "image"]}


def test_image_route_problem_names_the_route_when_nothing_can_see() -> None:
    problem = image_route_problem(_settings(cheap=TEXT_ONLY), None)
    assert problem is not None and "'cheap'" in problem


def test_image_route_problem_is_satisfied_by_either_vision_setting() -> None:
    settings = _settings(cheap=TEXT_ONLY, seer=SEEING)
    assert image_route_problem(settings, SimpleNamespace(id=None, vision_model="seer")) is None
    settings.default_vision_model_id = "seer"
    assert image_route_problem(settings, None) is None


def test_image_route_problem_follows_a_per_request_model_override() -> None:
    settings = _settings(cheap=TEXT_ONLY, seer=SEEING)
    assert image_route_problem(settings, None, model_id="seer") is None


def test_image_route_problem_says_nothing_about_an_unvouched_route() -> None:
    settings = _settings(cheap={"provider": "scripted", "model": "mystery-model"})
    assert image_route_problem(settings, None) is None


@pytest.fixture
def scripted() -> Any:
    from felix_ai import registry

    built: list[ScriptedClient] = []
    queue = [ScriptedTurn(content="one"), ScriptedTurn(content="two"), ScriptedTurn(content="three")]

    def factory(model_id: str, route: Any, spec: Any, settings: Any) -> ScriptedClient:
        client = ScriptedClient(model_id=model_id, route=route, script=queue)
        built.append(client)
        return client

    saved = dict(registry._providers)
    register_model_provider("scripted", factory)
    yield built
    registry._providers.clear()
    registry._providers.update(saved)


async def test_build_model_sends_images_to_the_vision_route_and_text_to_the_primary(scripted: Any) -> None:
    settings = _settings(cheap=TEXT_ONLY, seer=SEEING)
    model = build_model(settings, SimpleNamespace(id=None, vision_model="seer"))
    assert isinstance(model, _VisionRoutingClient)

    await model.chat([ChatMessage(role="user", content="hi")], [])
    assert model.model_id == "cheap"
    await model.chat([ChatMessage(role="user", content="look", attachments=[ImageAttachment(url=PNG)])], [])
    # Metering reads these off the client after the call; they must name the route that answered.
    assert model.model_id == "seer"
    assert model.route.modalities == ("text", "image")

    by_route = {c.model_id: c.calls for c in scripted}
    assert by_route == {"cheap": ["chat"], "seer": ["chat"]}


async def test_build_model_leaves_a_seeing_primary_uncomposed(scripted: Any) -> None:
    model = build_model(_settings(cheap=SEEING, seer=SEEING), SimpleNamespace(id=None, vision_model="seer"))
    assert not isinstance(model, _VisionRoutingClient)


def test_a_vision_route_that_cannot_see_is_a_build_error(scripted: Any) -> None:
    with pytest.raises(ValueError, match="does not accept images either"):
        build_model(
            _settings(cheap=TEXT_ONLY, blind=TEXT_ONLY), SimpleNamespace(id=None, vision_model="blind")
        )


async def test_the_leaf_swaps_images_for_a_note_on_a_text_only_route(scripted: Any) -> None:
    """The floor under the routing: a fallback or a tool image reaching a text-only route."""
    model = build_model(_settings(cheap=TEXT_ONLY), SimpleNamespace(id=None))
    seen: list[list[ChatMessage]] = []
    inner = scripted[0]
    original = inner.chat

    async def spy(messages: list[ChatMessage], *args: Any, **kwargs: Any) -> Any:
        seen.append(list(messages))
        return await original(messages, *args, **kwargs)

    inner.chat = spy  # type: ignore[method-assign]
    await model.chat([ChatMessage(role="user", content="look", attachments=[ImageAttachment(url=PNG)])], [])
    (sent,) = seen[0]
    assert sent.attachments is None
    assert "route 'cheap' does not accept images" in sent.content
