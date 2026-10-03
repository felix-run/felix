"""Which routes can see an image, and what happens to one sent where it cannot be seen."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from felix.config import Settings
from felix.patterns.model import build_model, parse_model_routes, record_model_usage
from felix.patterns.model_composites import _VisionRoutingClient
from felix.patterns.model_registry import register_model_provider
from felix.patterns.model_vision import (
    route_accepts_images,
    unseeable_image_problem,
    vision_plan,
    without_images,
)
from felix_ai.catalog import accepts_images
from felix_ai.providers.scripted import ScriptedClient, ScriptedTurn
from felix_ai.types import ChatMessage, ContentBlock, ImageAttachment, ModelChatResult, ModelRoute

PNG = "data:image/png;base64,iVBORw0KGgo="


def _looking() -> ChatMessage:
    return ChatMessage(role="user", content="look", attachments=[ImageAttachment(url=PNG)])


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


def test_without_images_counts_an_image_once_not_on_every_later_call(monkeypatch: pytest.MonkeyPatch) -> None:
    counted: list[float] = []
    monkeypatch.setattr(
        "felix.patterns.model_vision.record_counter",
        lambda name, labels=None, value=1, **_: counted.append(value),
    )
    first = [_looking()]
    without_images(first, "cheap")
    later = [*first, ChatMessage(role="assistant", content="ok"), ChatMessage(role="user", content="and?")]
    without_images(later, "cheap")
    assert counted == [1]


def _settings(**routes: dict[str, Any]) -> Settings:
    # `default_vision_model_id` pinned: `Settings` reads the repo `.env`, and an operator who
    # took doctor's advice would otherwise change what every test here asserts.
    return Settings(model_routes=json.dumps(routes), default_model_id="cheap", default_vision_model_id="")


TEXT_ONLY = {"provider": "scripted", "model": "claude-sonnet-5", "modalities": ["text"]}
SEEING = {"provider": "scripted", "model": "claude-sonnet-5", "modalities": ["text", "image"]}
UNVOUCHED = {"provider": "scripted", "model": "mystery-model"}


def test_the_plan_names_the_route_when_nothing_can_see() -> None:
    plan = vision_plan(_settings(cheap=TEXT_ONLY), None)
    assert plan.problem is not None and "'cheap'" in plan.problem
    assert not plan.misconfigured, "no vision route is a refusal for image turns, not a build error"


def test_the_plan_is_satisfied_by_either_vision_setting() -> None:
    settings = _settings(cheap=TEXT_ONLY, seer=SEEING)
    assert vision_plan(settings, SimpleNamespace(id=None, vision_model="seer")).vision_id == "seer"
    settings.default_vision_model_id = "seer"
    assert vision_plan(settings, None).vision_id == "seer"


def test_the_plan_follows_a_per_request_model_override() -> None:
    settings = _settings(cheap=TEXT_ONLY, seer=SEEING)
    assert vision_plan(settings, None, model_id="seer").problem is None


def test_the_plan_says_nothing_about_an_unvouched_route() -> None:
    plan = vision_plan(_settings(cheap=UNVOUCHED, seer=SEEING), SimpleNamespace(id=None, vision_model="seer"))
    assert plan.problem is None and plan.vision_id is None


@pytest.mark.parametrize(
    ("vision", "reason"),
    [("typo", "is not in FELIX_MODEL_ROUTES"), ("blind", "does not accept images either")],
)
def test_a_vision_route_that_cannot_serve_is_misconfigured(vision: str, reason: str) -> None:
    plan = vision_plan(
        _settings(cheap=TEXT_ONLY, blind=TEXT_ONLY), SimpleNamespace(id=None, vision_model=vision)
    )
    assert plan.misconfigured and plan.problem is not None and reason in plan.problem


def test_the_request_check_reads_the_manifest_model() -> None:
    manifest = SimpleNamespace(spec=SimpleNamespace(model=SimpleNamespace(id=None, vision_model="seer")))
    settings = _settings(cheap=TEXT_ONLY, seer=SEEING)
    assert unseeable_image_problem(manifest, [_looking()], settings) is None
    assert unseeable_image_problem(SimpleNamespace(spec=None), [_looking()], settings) is not None
    assert (
        unseeable_image_problem(
            SimpleNamespace(spec=None), [ChatMessage(role="user", content="hi")], settings
        )
        is None
    )


@pytest.fixture
def scripted() -> Any:
    from felix_ai import registry

    built: list[ScriptedClient] = []
    queue = [ScriptedTurn(content=str(i)) for i in range(4)]

    def factory(model_id: str, route: Any, spec: Any, settings: Any) -> ScriptedClient:
        client = ScriptedClient(model_id=model_id, route=route, script=queue)
        built.append(client)
        return client

    saved = dict(registry._providers)
    register_model_provider("scripted", factory)
    yield built
    registry._providers.clear()
    registry._providers.update(saved)


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
async def test_images_go_to_the_vision_route_and_text_to_the_primary(scripted: Any, how: str) -> None:
    model = build_model(
        _settings(cheap=TEXT_ONLY, seer=SEEING), SimpleNamespace(id=None, vision_model="seer")
    )
    assert isinstance(model, _VisionRoutingClient)

    seen = await _drain(model, how, [_looking()])
    text = await _drain(model, how, [ChatMessage(role="user", content="hi")])

    assert (seen.served_model_id, text.served_model_id) == ("seer", None)
    assert {c.model_id: c.calls for c in scripted} == {"cheap": [how], "seer": [how]}


async def test_a_vision_turn_is_metered_under_the_route_that_answered(
    scripted: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    metered: list[tuple[str | None, str | None]] = []
    monkeypatch.setattr(
        "felix.patterns.model.record_usage",
        lambda result, **kw: metered.append((kw["model_id"], kw["wire_model_id"])) or {},
    )
    routes = {"cheap": {**TEXT_ONLY, "model": "@cf/openai/gpt-oss-120b"}, "seer": SEEING}
    model = build_model(_settings(**routes), SimpleNamespace(id=None, vision_model="seer"))

    record_model_usage(await model.chat([_looking()], []), model, manifest_id="m")
    record_model_usage(await model.chat([ChatMessage(role="user", content="hi")], []), model, manifest_id="m")
    assert metered == [("seer", "claude-sonnet-5"), ("cheap", "@cf/openai/gpt-oss-120b")]


async def test_a_vision_route_that_cannot_stream_a_turn_is_settled_with_chat(scripted: Any) -> None:
    primary = ScriptedClient(model_id="cheap", route=ModelRoute("scripted", "x"), script=[ScriptedTurn()])

    class ChatOnly:
        model_id = "seer"
        route = ModelRoute("scripted", "y")
        calls: list[str] = []

        async def chat(self, messages: Any, tools: Any, opts: Any = None) -> ModelChatResult:
            self.calls.append("chat")
            return ModelChatResult(message=ChatMessage(role="assistant", content="a logo"))

    vision = ChatOnly()
    result = await _drain(
        _VisionRoutingClient(primary=primary, vision=vision, model_id="cheap", route=primary.route),
        "stream_turn",
        [_looking()],
    )  # type: ignore[arg-type]
    assert vision.calls == ["chat"] and result.served_model_id == "seer"


async def test_an_unvouched_primary_is_left_alone_and_the_vision_route_never_built(scripted: Any) -> None:
    model = build_model(
        _settings(cheap=UNVOUCHED, seer=SEEING), SimpleNamespace(id=None, vision_model="seer")
    )
    assert not isinstance(model, _VisionRoutingClient)
    assert [c.model_id for c in scripted] == ["cheap"]


async def test_a_seeing_primary_is_left_alone(scripted: Any) -> None:
    model = build_model(_settings(cheap=SEEING, seer=SEEING), SimpleNamespace(id=None, vision_model="seer"))
    assert not isinstance(model, _VisionRoutingClient)


@pytest.mark.parametrize("vision", ["typo", "blind"])
def test_a_vision_route_that_cannot_serve_is_a_build_error(scripted: Any, vision: str) -> None:
    with pytest.raises(ValueError, match="vision model route"):
        build_model(
            _settings(cheap=TEXT_ONLY, blind=TEXT_ONLY), SimpleNamespace(id=None, vision_model=vision)
        )


@pytest.mark.parametrize("how", ["chat", "stream_turn"])
async def test_the_leaf_swaps_images_for_a_note_on_a_text_only_route(scripted: Any, how: str) -> None:
    """The floor under the routing: a fallback or a tool image reaching a text-only route.

    Both entry points, because react takes `stream_turn` whenever a provider has one -- the
    path production runs is not the `chat` one.
    """
    model = build_model(_settings(cheap=TEXT_ONLY), SimpleNamespace(id=None))
    seen: list[list[ChatMessage]] = []
    inner = scripted[0]
    original = getattr(inner, how)

    def spy(messages: list[ChatMessage], *args: Any, **kwargs: Any) -> Any:
        seen.append(list(messages))
        return original(messages, *args, **kwargs)

    setattr(inner, how, spy)
    await _drain(model, how, [_looking()])
    (sent,) = seen[0]
    assert sent.attachments is None
    assert "route 'cheap' does not accept images" in sent.content
