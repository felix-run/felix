"""Images and the routes that cannot see them.

A route whose model is vouched text-only (`felix_ai.catalog.accepts_images` is False) used to
be sent every image a conversation carried, and answered that it could not see one -- which
reads as the client's fault. Two things replace that here:

- `_VisionRoutingClient` sends a call that carries an image to `spec.model.vision_model`
  (or `FELIX_DEFAULT_VISION_MODEL_ID`) and every other call to the primary, so a cheap text
  route can stay the default for an agent that is sometimes shown a picture.
- `without_images` is the floor under that: the leaf client in `patterns/model.py` swaps each
  image for a line saying it was omitted, so a text-only route that has no vision route --
  a fallback, a planner, an image a tool returned mid-run -- says so in the transcript
  instead of being handed bytes it ignores.

A user turn that would reach that floor is refused before streaming starts instead
(`image_route_problem`), because a person who attached a picture should hear that it cannot
be read, not get an answer about something else.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, replace
from typing import Any

from felix_ai.catalog import accepts_images
from felix_ai.types import (
    ChatMessage,
    ContentBlock,
    ModelChatOptions,
    ModelChatResult,
    ModelClient,
    ModelRoute,
    StreamDelta,
    ToolSchema,
    supports_stream_turn,
)

from felix.observability.metrics import record_counter
from felix.patterns.model_composites import _settled_stream

logger = logging.getLogger("felix.patterns.model_vision")


def route_accepts_images(route: ModelRoute | None) -> bool | None:
    """`accepts_images` for a route: its own `modalities` first, then the catalog."""
    if route is None:
        return None
    return accepts_images(route.model, getattr(route, "modalities", None))


def message_has_images(m: ChatMessage) -> bool:
    if m.attachments:
        return True
    return any(b.type != "text" for b in m.content_blocks or ())


def carries_images(messages: Sequence[ChatMessage]) -> bool:
    return any(message_has_images(m) for m in messages)


def without_images(messages: list[ChatMessage], route_name: str) -> list[ChatMessage]:
    """`messages` with every image replaced by a line naming the route that could not see it.

    Returns the list untouched when nothing carries an image, which is nearly every call.
    """
    if not carries_images(messages):
        return messages
    note = f"[image omitted: model route '{route_name}' does not accept images]"
    out: list[ChatMessage] = []
    stripped = 0
    for m in messages:
        if not message_has_images(m):
            out.append(m)
            continue
        if m.content_blocks:
            blocks: list[ContentBlock] = []
            for b in m.content_blocks:
                if b.type == "text":
                    blocks.append(b)
                else:
                    stripped += 1
                    blocks.append(ContentBlock(type="text", text=note))
            out.append(replace(m, content_blocks=blocks, attachments=None))
        else:
            count = len(m.attachments or ())
            stripped += count
            text = "\n".join([m.content, *([note] * count)]) if m.content else "\n".join([note] * count)
            out.append(replace(m, content=text, attachments=None))
    record_counter("felix_model_images_omitted", {"model": route_name}, stripped)
    logger.warning("omitted %d image(s) for text-only model route %s", stripped, route_name)
    return out


@dataclass
class _VisionRoutingClient:
    """The primary for text, the vision route for any call that carries an image.

    `model_id`, `route` and `price_override` follow whichever client answered the most
    recent call, because `record_model_usage` reads them off the client *after* the call:
    left fixed at the primary's, a vision turn would be metered and priced as the cheap
    text model it never reached. Within one run that is stable -- once an image is in the
    history, every later call carries it and goes to the vision route.
    """

    primary: ModelClient
    vision: ModelClient
    _served: ModelClient | None = None

    @property
    def model_id(self) -> str:
        return (self._served or self.primary).model_id

    @property
    def route(self) -> ModelRoute:
        return (self._served or self.primary).route

    @property
    def price_override(self) -> dict[str, float] | None:
        return getattr(self._served or self.primary, "price_override", None)

    def _pick(self, messages: Sequence[ChatMessage]) -> ModelClient:
        client = self.vision if carries_images(messages) else self.primary
        if client is self.vision:
            record_counter(
                "felix_model_switch",
                {"from": self.primary.model_id, "to": self.vision.model_id, "reason": "vision"},
            )
        self._served = client
        return client

    async def chat(
        self,
        messages: list[ChatMessage],
        tools: Sequence[ToolSchema],
        opts: ModelChatOptions | None = None,
    ) -> ModelChatResult:
        return await self._pick(messages).chat(messages, tools, opts)

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: Sequence[ToolSchema],
        opts: ModelChatOptions | None = None,
    ) -> AsyncIterator[str]:
        async for chunk in self._pick(messages).stream(messages, tools, opts):
            yield chunk

    async def stream_turn(
        self,
        messages: list[ChatMessage],
        tools: Sequence[ToolSchema],
        opts: ModelChatOptions | None = None,
    ) -> AsyncIterator[StreamDelta | ModelChatResult]:
        # Defined unconditionally, like the resilience composites, so a client that cannot
        # stream a turn is settled here with `chat` rather than leaving the caller to notice.
        client = self._pick(messages)
        if supports_stream_turn(client):
            async for item in client.stream_turn(messages, tools, opts):
                yield item
            return
        for item in _settled_stream(await client.chat(messages, tools, opts)):
            yield item


def vision_route_id(settings: Any, spec: Any) -> str:
    """The logical route images go to when the primary cannot take them, or ''."""
    return str(getattr(spec, "vision_model", None) or getattr(settings, "default_vision_model_id", "") or "")


def with_vision_route(client: ModelClient, settings: Any, spec: Any, build_one: Any) -> ModelClient:
    """`client` composed with a vision route, when the primary is text-only and one is set.

    A primary the catalog cannot vouch for either way is left alone: rerouting it would move
    traffic off a route that may well see images, on the strength of a guess.
    """
    if route_accepts_images(client.route) is not False:
        return client
    vision_id = vision_route_id(settings, spec)
    if not vision_id:
        return client
    vision = build_one(settings, spec, vision_id)
    if route_accepts_images(vision.route) is False:
        raise ValueError(
            f"vision model route '{vision_id}' ({vision.route.model}) does not accept images either"
        )
    return _VisionRoutingClient(primary=client, vision=vision)


def image_route_problem(settings: Any, spec: Any, model_id: str | None = None) -> str | None:
    """Why a turn carrying an image cannot be answered by this model spec, or None.

    Resolved from routes and the catalog alone, without building a client, so a request
    route can refuse before it starts streaming -- a raise mid-stream holds the connection.
    `model_id` is a per-request override of `spec.id` (the allowlisted `model` on `/chat`).
    """
    from felix.patterns.model import parse_model_routes

    routes = parse_model_routes(settings)
    primary_id = model_id or getattr(spec, "id", None) or settings.default_model_id
    if route_accepts_images(routes.get(primary_id)) is not False:
        return None
    vision_id = vision_route_id(settings, spec)
    if vision_id and route_accepts_images(routes.get(vision_id)) is not False:
        return None
    wire = routes[primary_id].model
    return (
        f"model route '{primary_id}' ({wire}) does not accept images; set spec.model.vision_model "
        "or FELIX_DEFAULT_VISION_MODEL_ID to a route that does"
    )


__all__ = [
    "carries_images",
    "image_route_problem",
    "message_has_images",
    "route_accepts_images",
    "vision_route_id",
    "with_vision_route",
    "without_images",
]
