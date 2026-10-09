"""Inbound message screening — injection markers, optional LLM, input PII."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from felix.config import Settings
from felix.governance.content_screening import screen_content
from felix.governance.image_screening import INGEST, REPLAY, TOOL, ImageScreener, ImageSurface, has_images
from felix.governance.pii import redact_pii_async
from felix.governance.screening import (
    INJECTION_THRESHOLD,
    MAX_SCREEN_CHUNKS,
    SCREEN_CHARS,
    InboundScreeningError,
    ScreenResult,
    input_pii_enabled,
    note_screening,
    screen_chunks,
    screen_for_injection,
    screening_decider,
    settle_screening,
)
from felix.governance.tool_screening import (
    MAX_ARGUMENT_STRINGS,
    screen_output_schema,
    screen_tool_arguments,
    screen_tool_output,
)
from felix.manifests.schema import Manifest
from felix.patterns.types import copy_agent_surface

if TYPE_CHECKING:
    from felix.decisions import MeteredDecider

logger = logging.getLogger("felix.governance.inbound")


def _message_text(msg: Any) -> str:
    if isinstance(msg, dict):
        content = msg.get("content")
    else:
        content = getattr(msg, "content", None)
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            elif isinstance(block, str):
                parts.append(block)
            else:
                text = getattr(block, "text", None)
                if text:
                    parts.append(str(text))
        return "\n".join(parts)
    return str(content)


def _set_message_text(msg: Any, text: str) -> Any:
    """Put the screened text where the model will actually read it.

    Writing `.content` alone was not enough, and the gap was silent. A multimodal message
    carries its text in `content_blocks` (or `attachments`), and *both* wire formats prefer
    those over `.content` — so on an image turn, PII redaction and the `[quarantined]`
    substitution were computed, audited as applied, and then bypassed: the model saw the
    caller's original text. A control that reports success and changes nothing.

    The blocks are only rebuilt when screening actually changed the text, so an ordinary turn
    keeps the caller's interleaving of text and images exactly as sent. When it did change,
    the screened text becomes one leading text block and the images follow in their original
    order — text and images may end up differently interleaved than the caller wrote them,
    which is the right trade against showing the model what was meant to be redacted.
    """
    if isinstance(msg, dict):
        out = dict(msg)
        out["content"] = text
        return out

    updates: dict[str, Any] = {"content": text}
    if text != getattr(msg, "content", text):
        blocks = getattr(msg, "content_blocks", None)
        if blocks:
            from felix_ai.types import ContentBlock

            updates["content_blocks"] = [ContentBlock(type="text", text=text)] + [
                b for b in blocks if b.type != "text"
            ]
        elif getattr(msg, "attachments", None):
            # The older shape carries no text of its own — the text is `.content`, which is
            # already screened above — so the attachments ride along untouched.
            pass

    if hasattr(msg, "model_copy"):
        return msg.model_copy(update=updates)
    import contextlib

    with contextlib.suppress(Exception):
        for field, value in updates.items():
            setattr(msg, field, value)
    return msg


def _role_of(msg: Any) -> str:
    if isinstance(msg, dict):
        return str(msg.get("role") or "")
    return str(getattr(msg, "role", "") or "")


async def apply_inbound_screening(
    manifest: Manifest,
    messages: list[Any],
    settings: Settings,
) -> list[Any]:
    """Screen user turns for injection + optional input PII. May rewrite content."""
    screening = manifest.spec.content_screening
    guardrails = manifest.spec.guardrails
    pii_on_input = input_pii_enabled(guardrails)
    content_on = bool(screening.enabled)
    if not content_on and not pii_on_input:
        return messages

    decider_down = False
    try:
        decider = screening_decider(manifest, settings) if content_on else None
    except Exception:
        # Unavailable, like a model screener that cannot run: `on_flag` decides, and under
        # quarantine every user turn is quarantined — never admitted unscreened.
        note_screening(manifest, "turn", "unavailable")
        if screening.on_flag == "block":
            raise InboundScreeningError("content_screening_unavailable:decider", status_code=503) from None
        decider, decider_down = None, True
    images = (
        _image_screener(manifest, settings, INGEST, decider, decider_down)
        if content_on and images_screened(manifest)
        else None
    )
    out: list[Any] = []
    for msg in messages:
        if _role_of(msg) != "user":
            out.append(msg)
            continue
        text = _message_text(msg)
        if text:
            screened = await _screen_turn_text(manifest, text, settings, decider, decider_down=decider_down)
            if screened != text:
                msg = _set_message_text(msg, screened)
        if images is not None and has_images(msg):
            msg = await images.screen(msg)
        out.append(msg)
    return out


def images_screened(manifest: Manifest) -> bool:
    screening = manifest.spec.content_screening
    return bool(screening.enabled and screening.image_model.strip())


def _image_screener(
    manifest: Manifest,
    settings: Settings,
    surface: ImageSurface,
    decider: MeteredDecider | None,
    decider_down: bool,
) -> ImageScreener:
    """One screening pass's `ImageScreener`. Callers check `images_screened` first."""
    screening = manifest.spec.content_screening

    async def screen_text(transcript: str) -> ScreenResult:
        return await _screen_transcript(manifest, transcript, settings, decider)

    # What `screen_text`'s answer depends on besides the transcript: the scorer, and whether
    # this manifest's decider is asked. The marker scan and the threshold are constants.
    decider_id = manifest.spec.decider.id if screening.decider else ""
    return ImageScreener(
        manifest=manifest,
        settings=settings,
        surface=surface,
        screen_text=screen_text,
        verdict_key=f"{screening.model.strip()}\0{decider_id}",
        decider_down=decider_down,
    )


def tool_image_screener(manifest: Manifest, settings: Settings | None) -> Callable[[], ImageScreener] | None:
    """`replay_screener`'s factory for images a tool returns: same verdicts, the TOOL surface."""
    return _screener_factory(manifest, settings, TOOL)


def replay_screener(manifest: Manifest, settings: Settings | None) -> Callable[[], ImageScreener] | None:
    """A factory of replay screeners for one compile, or `None` when it screens no images.

    The builder wraps the session strategy with it (`screen_session_strategy`), so every render
    of history — a turn's assembly, compaction, overflow recovery, a plugin pattern's own —
    has its images screened. A factory because each render is its own pass with its own
    budget, and the decider is bound per pass the way the inbound screen binds it.
    """
    return _screener_factory(manifest, settings, REPLAY)


def _screener_factory(
    manifest: Manifest, settings: Settings | None, surface: ImageSurface
) -> Callable[[], ImageScreener] | None:
    if not images_screened(manifest):
        return None
    if settings is None:
        from felix.config import get_settings

        settings = get_settings()
    bound = settings

    def make() -> ImageScreener:
        try:
            decider, down = screening_decider(manifest, bound), False
        except Exception:
            decider, down = None, True
        return _image_screener(manifest, bound, surface, decider, down)

    return make


async def _screen_turn_text(
    manifest: Manifest,
    text: str,
    settings: Settings,
    decider: MeteredDecider | None,
    *,
    decider_down: bool,
) -> str:
    """One user turn's text, screened: returned as-is, replaced by a `[quarantined]` note, or
    PII-redacted. Raises `InboundScreeningError` where `on_flag: block` refuses the turn."""
    screening = manifest.spec.content_screening
    guardrails = manifest.spec.guardrails
    if screening.enabled:
        # Whether *screening* replaced the text. Asked of a flag, not of the text: a turn the
        # caller began with "[quarantined]" is still the caller's, and testing the prefix let
        # it skip the model and decider scoring.
        quarantined = False
        if (await screen_content(text, settings=settings, block_on_injection=True, redact_pii=False)).denied:
            settle_screening(manifest, "turn", "flagged", error="content_screening_denied", status_code=422)
            text, quarantined = "[quarantined] user input flagged as potentially hostile", True
        if decider_down:
            text, quarantined = "[quarantined] user input could not be screened", True
        model_id = (screening.model or "").strip()
        if (model_id or decider) and len(text) > MAX_SCREEN_CHUNKS * SCREEN_CHARS:
            # Windowing removed the truncation bypass; this keeps it from becoming an
            # amplifier — a body-limit-sized turn is not screened one window at a time.
            settle_screening(manifest, "turn", "oversize", error="turn_too_large", status_code=422)
            text, quarantined = "[quarantined] user input too long to screen", True
        if (model_id or decider) and not quarantined:
            result = await screen_chunks(settings, text, model_id, decider)
            if result.unavailable:
                # A control that cannot run has not cleared anything. Honour on_flag
                # rather than silently admitting the turn.
                settle_screening(
                    manifest,
                    "turn",
                    "unavailable",
                    error=f"content_screening_unavailable:{result.reason}",
                    status_code=503,
                )
                text = "[quarantined] user input could not be screened"
            elif result.flagged:
                # The score stays in the log: returned, it is a threshold oracle.
                logger.info("inbound screening flagged a turn score=%.2f", result.score)
                settle_screening(
                    manifest, "turn", "flagged", error="content_screening_denied", status_code=422
                )
                text = "[quarantined] user input flagged by model screener"

    if input_pii_enabled(guardrails):
        result = await redact_pii_async(text)
        if result.matched:
            note_screening(manifest, "turn", "denied" if guardrails.block_on_match else "redacted")
            if guardrails.block_on_match:
                raise InboundScreeningError("pii_blocked", status_code=422)
            text = result.text
    return text


async def _screen_transcript(
    manifest: Manifest, text: str, settings: Settings, decider: MeteredDecider | None
) -> ScreenResult:
    """The verdict on an image's transcript, held to the rule the turn's own text is.

    A verdict rather than a rewrite, because the image, not the text, is what gets removed.
    The marker scan reads the whole transcript whatever its length; the length ceiling applies
    only when a paid scorer would read it window by window, as it does for text.
    """
    screening = manifest.spec.content_screening
    if (await screen_content(text, settings=settings, block_on_injection=True, redact_pii=False)).denied:
        return ScreenResult(score=1.0, reason="marker")
    model_id = (screening.model or "").strip()
    if not (model_id or decider):
        return ScreenResult(score=0.0)
    if len(text) > MAX_SCREEN_CHUNKS * SCREEN_CHARS:
        return ScreenResult(available=False, reason="oversize")
    return await screen_chunks(settings, text, model_id, decider)


INBOUND_SCREENED_EXTRA = "inbound_screened"


class InboundScreeningAgent:
    """The compiled agent, with the user turn screened on every way in.

    `apply_inbound_screening` used to be a call each entrypoint had to remember: /chat,
    /v1 and A2A did, and cron jobs, eval items, /chat/continue and a resumed durable
    fiber did not. Wrapping the agent in the compile means there is no entrypoint to
    forget — anything that runs the agent runs the screen. An `InboundScreeningError`
    propagates to the caller, which maps it (422/503 on HTTP, an error run on cron, an
    error score on eval, a failed fiber).
    """

    def __init__(self, inner: Any, manifest: Manifest, settings: Settings) -> None:
        self._inner = inner
        self._manifest = manifest
        self._settings = settings
        copy_agent_surface(self, inner, manifest_id=manifest.metadata.name)

    async def _screened(self, input: Any) -> Any:
        from dataclasses import replace

        from felix.context import try_get_context

        ctx = try_get_context()
        # Consumed, not read: the mark means "this turn, screened at the route". A sub-agent
        # compiled in the same context is a different agent with its own manifest, and
        # screens what its parent hands it.
        if ctx is not None and ctx.extras.pop(INBOUND_SCREENED_EXTRA, False):
            return input
        messages = await apply_inbound_screening(self._manifest, list(input.messages), self._settings)
        return replace(input, messages=messages)

    async def invoke(self, input: Any) -> Any:
        return await self._inner.invoke(await self._screened(input))

    async def stream_events(self, input: Any) -> Any:
        screened = await self._screened(input)
        async for item in self._inner.stream_events(screened):
            yield item


def inbound_controls_enabled(manifest: Manifest) -> bool:
    return bool(manifest.spec.content_screening.enabled) or input_pii_enabled(manifest.spec.guardrails)


def apply_inbound_controls(agent: Any, manifest: Manifest, settings: Settings | None) -> Any:
    """The compile slot: wrap when the manifest screens input, else hand the agent back."""
    if not inbound_controls_enabled(manifest):
        return agent
    if settings is None:
        from felix.config import get_settings

        settings = get_settings()
    return InboundScreeningAgent(agent, manifest, settings)


__all__ = [
    "INBOUND_SCREENED_EXTRA",
    # Re-exported from `screening` and `tool_screening`, where they live now, for the callers
    # and plugins that import them from here. A patch on one of these names here reaches only
    # `inbound`'s own callers; replace a scorer for a test in the module that defines it.
    "INJECTION_THRESHOLD",
    "MAX_ARGUMENT_STRINGS",
    "MAX_SCREEN_CHUNKS",
    "SCREEN_CHARS",
    "InboundScreeningAgent",
    "InboundScreeningError",
    "ScreenResult",
    "apply_inbound_controls",
    "apply_inbound_screening",
    "images_screened",
    "inbound_controls_enabled",
    "input_pii_enabled",
    "replay_screener",
    "screen_for_injection",
    "screen_output_schema",
    "screen_tool_arguments",
    "screen_tool_output",
    "screening_decider",
]
