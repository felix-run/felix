"""Images and the routes that cannot see them.

A route whose model is vouched text-only (`felix_ai.catalog.accepts_images` is False) used to
be sent every image a conversation carried, and answered that it could not see one -- which
reads as the client's fault. What replaces that is decided once, by `vision_plan`, from the
route table alone:

- With a vision route (`spec.model.vision_model`, else `FELIX_DEFAULT_VISION_MODEL_ID`),
  `build_model` composes `_VisionRoutingClient` (in `model_composites.py`, beside the other
  composites) so a call carrying an image goes there and every other call to the primary.
- Without one, a request route refuses a user turn carrying an image before it streams
  (`unseeable_image_problem`), because a person who attached a picture should hear that it
  cannot be read rather than get an answer about something else.
- Under both, `without_images` is the floor: the leaf client in `patterns/model.py` swaps each
  image for a line saying it was omitted, so a text-only route reached anyway -- a fallback, an
  image a tool returned mid-run -- says so instead of being handed bytes it ignores.

The 422 and the build read the same plan, so they cannot disagree about a route: a vision id
that is not routed is a problem for both, not a pass for one and a build error for the other.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from felix_ai.catalog import accepts_images
from felix_ai.types import ChatMessage, ContentBlock, ModelRoute, is_image_part

from felix.observability.metrics import record_counter

if TYPE_CHECKING:
    from felix.config import Settings

logger = logging.getLogger("felix.patterns.model_vision")


def route_accepts_images(route: ModelRoute | None) -> bool | None:
    """`accepts_images` for a route: its own `modalities` first, then the catalog."""
    if route is None:
        return None
    return accepts_images(route.model, route.modalities)


def message_has_images(m: ChatMessage) -> bool:
    return any(is_image_part(p) for p in (*(m.content_blocks or ()), *(m.attachments or ())))


def carries_images(messages: Sequence[ChatMessage]) -> bool:
    return any(message_has_images(m) for m in messages)


def _image_count(m: ChatMessage) -> int:
    if m.content_blocks:
        return sum(is_image_part(b) for b in m.content_blocks)
    return sum(is_image_part(a) for a in m.attachments or ())


def without_images(messages: list[ChatMessage], route_name: str) -> list[ChatMessage]:
    """`messages` with every image replaced by a line naming the route that could not see it.

    Returns the list untouched when nothing carries an image, which is nearly every call.
    Every call strips the whole history again, but only images that arrived since the last
    assistant turn are counted and logged: an image stays in the history for the rest of the
    run, and counting it per call would report one picture as many.
    """
    if not carries_images(messages):
        return messages
    note = f"[image omitted: model route '{route_name}' does not accept images]"
    last_reply = max((i for i, m in enumerate(messages) if m.role == "assistant"), default=-1)
    out: list[ChatMessage] = []
    new = 0
    for i, m in enumerate(messages):
        if not message_has_images(m):
            out.append(m)
            continue
        if i > last_reply:
            new += _image_count(m)
        if m.content_blocks:
            blocks = [
                ContentBlock(type="text", text=note) if is_image_part(b) else b for b in m.content_blocks
            ]
            # `inline_parts` renders `content_blocks` and ignores `attachments` when both are
            # set, so the attachments carried nothing to the wire and need no note of their own.
            out.append(replace(m, content_blocks=blocks, attachments=None))
        else:
            count = sum(is_image_part(a) for a in m.attachments or ())
            text = "\n".join([*([m.content] if m.content else []), *([note] * count)])
            out.append(replace(m, content=text, attachments=None))
    if new:
        record_counter("felix_model_images_omitted", {"model": route_name}, new)
        logger.warning("omitted %d image(s) for text-only model route %s", new, route_name)
    return out


def caller_images_on_user_turns(messages: list[ChatMessage]) -> list[ChatMessage]:
    """A request's messages with every image off a non-user turn removed.

    Inbound image screening reads user turns, because those are the caller's own words. A
    caller can also send history -- `role: tool`, `assistant` -- and an image there used to be
    dropped by both wires. Now the wires render a tool message's images, for the ones a tool
    Felix ran returned; one a caller wrote into a tool message would reach the model past
    every screen, `on_flag: block` and the remote-URL rule included. So it is removed here,
    at the door, where the only images that can be a caller's are on user turns.
    """
    out: list[ChatMessage] = []
    removed = 0
    for m in messages:
        if m.role == "user" or not message_has_images(m):
            out.append(m)
            continue
        removed += _image_count(m) or 1
        blocks = [b for b in m.content_blocks or () if not is_image_part(b)] or None
        out.append(replace(m, content_blocks=blocks, attachments=None))
    if removed:
        logger.warning("removed %d image(s) from caller-supplied non-user messages", removed)
    return out


@dataclass(frozen=True, slots=True)
class VisionPlan:
    """Which route answers an image for one model spec, decided from the route table.

    `vision_id` is set only when it should be composed: the primary is vouched text-only and
    a routed, image-capable vision route was named. `problem` says why an image cannot be
    seen. `misconfigured` marks a vision route that was named and cannot serve -- a build
    error, like an unroutable fallback -- as against no vision route at all, which only a
    turn carrying an image needs to hear about.
    """

    primary_id: str
    vision_id: str | None = None
    problem: str | None = None
    misconfigured: bool = False


def vision_plan(settings: Settings, spec: Any, model_id: str | None = None) -> VisionPlan:
    """The one place the image routing is decided. `model_id` overrides `spec.id` per request."""
    from felix.patterns.model import parse_model_routes

    routes = parse_model_routes(settings)
    primary_id = model_id or getattr(spec, "id", None) or settings.default_model_id
    primary = routes.get(primary_id)
    if primary is None or route_accepts_images(primary) is not False:
        # Can see, or cannot be vouched for either way -- rerouting the latter would move
        # traffic off a route that may well see images, on the strength of a guess. An
        # unrouted primary is `build_one_model`'s error to raise, not this one's.
        return VisionPlan(primary_id)
    vision_id = getattr(spec, "vision_model", None) or settings.default_vision_model_id
    if not vision_id:
        return VisionPlan(
            primary_id,
            problem=(
                f"model route '{primary_id}' ({primary.model}) does not accept images; set "
                "spec.model.vision_model or FELIX_DEFAULT_VISION_MODEL_ID to a route that does"
            ),
        )
    vision = routes.get(vision_id)
    if vision is None:
        problem = f"vision model route '{vision_id}' is not in FELIX_MODEL_ROUTES"
    elif route_accepts_images(vision) is False:
        problem = f"vision model route '{vision_id}' ({vision.model}) does not accept images either"
    else:
        return VisionPlan(primary_id, vision_id=vision_id)
    return VisionPlan(primary_id, problem=problem, misconfigured=True)


def unseeable_image_problem(
    manifest: Any, messages: Sequence[ChatMessage], settings: Settings, model_id: str | None = None
) -> str | None:
    """Why this turn's images cannot be seen by the agent `manifest` compiles to, or None.

    For request routes, before the agent is built and before a stream opens -- a raise
    mid-stream holds the connection. Each route wraps the answer in its own error envelope.
    """
    if not carries_images(messages):
        return None
    spec = getattr(getattr(manifest, "spec", None), "model", None)
    return vision_plan(settings, spec, model_id).problem


__all__ = [
    "VisionPlan",
    "caller_images_on_user_turns",
    "carries_images",
    "message_has_images",
    "route_accepts_images",
    "unseeable_image_problem",
    "vision_plan",
    "without_images",
]
