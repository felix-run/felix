"""Screening what reaches a tool, or leaves one, outside an agent turn's own inbound screen.

Tool output on its way back to the model, the arguments of a tool called directly over MCP,
and an output schema a caller supplies. Each is held to the injection screen a user turn
gets, with its own ceiling on how much work a single call may ask for.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from felix.config import Settings
from felix.governance.content_screening import screen_content
from felix.governance.pii import redact_pii_async
from felix.governance.screening import (
    MAX_SCREEN_CHUNKS,
    SCREEN_CHARS,
    InboundScreeningError,
    ScreenResult,
    input_pii_enabled,
    note_screening,
    screen_chunks,
    screening_decider,
)
from felix.manifests.schema import Manifest

if TYPE_CHECKING:
    from felix.decisions import MeteredDecider

logger = logging.getLogger("felix.governance.tool_screening")


def _decider_or_refuse(manifest: Manifest, settings: Settings) -> MeteredDecider | None:
    """`screening_decider` for the surfaces that refuse rather than quarantine."""
    try:
        return screening_decider(manifest, settings)
    except Exception:
        raise InboundScreeningError("content_screening_unavailable:decider", status_code=503) from None


async def screen_tool_output(
    settings: Settings, text: str, model_id: str, decider: MeteredDecider | None = None
) -> ScreenResult:
    """`screen_chunks` for tool output, with the same size ceiling a user turn has.

    Output beyond `MAX_SCREEN_CHUNKS` windows is not screened window by window — each
    window is a model call — and so is reported unavailable: `on_flag` then quarantines or
    blocks it, where it used to be screened on its first window and admitted.
    """
    if len(text) > MAX_SCREEN_CHUNKS * SCREEN_CHARS:
        return ScreenResult(available=False, reason="too_large_to_screen")
    return await screen_chunks(settings, text, model_id, decider)


def _strings_in(value: Any) -> list[str]:
    """Every string in an argument tree — values *and* keys, since a free-form map's keys
    reach the tool too."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [t for k, v in value.items() for t in (*_strings_in(k), *_strings_in(v))]
    if isinstance(value, list | tuple):
        return [t for v in value for t in _strings_in(v)]
    return []


def _keys_in(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [t for k, v in value.items() for t in ([k] if isinstance(k, str) else []) + _keys_in(v)]
    if isinstance(value, list | tuple):
        return [t for v in value for t in _keys_in(v)]
    return []


def _map_values(value: Any, fn: Any) -> Any:
    """Rewrite string *values* only: a key rewritten is a parameter renamed, and two keys
    rewritten to the same token would collapse into one."""
    if isinstance(value, str):
        return fn(value)
    if isinstance(value, dict):
        return {k: _map_values(v, fn) for k, v in value.items()}
    if isinstance(value, list):
        return [_map_values(v, fn) for v in value]
    if isinstance(value, tuple):
        return tuple(_map_values(v, fn) for v in value)
    return value


# An argument tree with more strings than this is not a tool call, and screening it is
# unbounded work an anonymous MCP client could ask for.
MAX_ARGUMENT_STRINGS = 256


async def _any_pii(texts: Any) -> bool:
    """Whether any of `texts` holds PII, stopping at the first that does."""
    for text in texts:
        if (await redact_pii_async(text)).matched:
            return True
    return False


async def screen_tool_arguments(
    manifest: Manifest, args: dict[str, Any], settings: Settings
) -> dict[str, Any]:
    """Screen the arguments of a tool call made directly over MCP.

    `tools/call` executes a governed tool without an agent turn, so the inbound screening
    a user turn gets never ran on what the remote client sent. Arguments cannot be
    quarantined the way a turn can — there is no model to warn — so a flagged argument
    refuses the call whatever `on_flag` says; `on_flag` only decides whether an
    *unavailable* model screener refuses (block) or lets the marker screen stand. The
    input PII guardrail applies as it does to a turn: block refuses, otherwise the
    string values are redacted in place, and PII in a *key* always refuses.
    """
    screening = manifest.spec.content_screening
    guardrails = manifest.spec.guardrails
    pii_on_input = input_pii_enabled(guardrails)
    if not screening.enabled and not pii_on_input:
        return args
    texts = [t for t in _strings_in(args) if t]
    if not texts:
        return args
    joined = "\n".join(texts)
    if len(texts) > MAX_ARGUMENT_STRINGS or len(joined) > MAX_SCREEN_CHUNKS * SCREEN_CHARS:
        note_screening(manifest, "tool_arguments", "oversize")
        raise InboundScreeningError("arguments_too_large", status_code=422)
    if screening.enabled:
        for text in texts:
            verdict = await screen_content(text, settings=settings, block_on_injection=True, redact_pii=False)
            if verdict.denied:
                note_screening(manifest, "tool_arguments", "denied")
                raise InboundScreeningError("content_screening_denied", status_code=422)
        model_id = (screening.model or "").strip()
        decider = _decider_or_refuse(manifest, settings)
        if model_id or decider:
            result = await screen_chunks(settings, joined, model_id, decider)
            if result.unavailable:
                note_screening(manifest, "tool_arguments", "unavailable")
                if screening.on_flag == "block":
                    raise InboundScreeningError(
                        f"content_screening_unavailable:{result.reason}", status_code=503
                    )
            elif result.flagged:
                logger.info("inbound screening flagged tool arguments score=%.2f", result.score)
                note_screening(manifest, "tool_arguments", "denied")
                raise InboundScreeningError("content_screening_denied", status_code=422)
    if pii_on_input:
        if await _any_pii(_keys_in(args)):
            note_screening(manifest, "tool_arguments", "denied")
            raise InboundScreeningError("pii_blocked", status_code=422)
        # Redacted up front, off the loop, then mapped: `_map_values` takes a plain function.
        # The first pass only collects the values it would rewrite (keys were checked above).
        values: list[str] = []
        _map_values(args, lambda text: values.append(text) or text)
        results = {text: await redact_pii_async(text) for text in dict.fromkeys(values)}
        matched = any(r.matched for r in results.values())
        redacted = _map_values(args, lambda text: results[text].text)
        if matched:
            note_screening(manifest, "tool_arguments", "denied" if guardrails.block_on_match else "redacted")
            if guardrails.block_on_match:
                raise InboundScreeningError("pii_blocked", status_code=422)
            return redacted
    return args


# A schema with more strings than this is not describing an answer shape. Lower than the tool
# bound because a schema's strings are titles and descriptions, not data.
MAX_SCHEMA_STRINGS = 128


async def screen_output_schema(manifest: Manifest, schema: dict[str, Any], settings: Settings) -> None:
    """Screen the text of a caller-supplied output schema, or refuse the request.

    `response_format` on `/v1` carries a JSON Schema from an unauthenticated client, and every
    string leaf of it — `title`, `description`, a property name — is serialised verbatim into
    the provider request. `apply_inbound_screening` iterates *messages*, and this rides on
    `model_options` instead, so the operator's inbound screening and input guardrails never saw
    it. A control switched on and bypassed at the field level.

    It is a worse channel than a user turn, not an equal one: options are resolved once and
    reused for the whole run, so this text sits in front of the model on *every* turn of the
    loop rather than on one.

    Refused rather than redacted, for the reason `screen_tool_arguments` refuses: there is no
    model to warn, so quarantining is not available — and rewriting a schema's description
    would silently change the contract the caller is holding. A schema that trips the screener
    is not a schema this deployment will enforce.
    """
    screening = manifest.spec.content_screening
    guardrails = manifest.spec.guardrails
    pii_on_input = input_pii_enabled(guardrails)
    if not screening.enabled and not pii_on_input:
        return
    texts = [t for t in _strings_in(schema) if t]
    if not texts:
        return
    joined = "\n".join(texts)
    if len(texts) > MAX_SCHEMA_STRINGS or len(joined) > MAX_SCREEN_CHUNKS * SCREEN_CHARS:
        note_screening(manifest, "output_schema", "oversize")
        raise InboundScreeningError("output_schema_too_large", status_code=422)
    if screening.enabled:
        for text in texts:
            verdict = await screen_content(text, settings=settings, block_on_injection=True, redact_pii=False)
            if verdict.denied:
                note_screening(manifest, "output_schema", "denied")
                raise InboundScreeningError("content_screening_denied", status_code=422)
        model_id = (screening.model or "").strip()
        decider = _decider_or_refuse(manifest, settings)
        if model_id or decider:
            result = await screen_chunks(settings, joined, model_id, decider)
            if result.unavailable:
                note_screening(manifest, "output_schema", "unavailable")
                if screening.on_flag == "block":
                    raise InboundScreeningError(
                        f"content_screening_unavailable:{result.reason}", status_code=503
                    )
            elif result.flagged:
                logger.info("inbound screening flagged an output schema score=%.2f", result.score)
                note_screening(manifest, "output_schema", "denied")
                raise InboundScreeningError("content_screening_denied", status_code=422)
    if pii_on_input and await _any_pii(texts):
        # Always a refusal, `block_on_match` or not: the redacted alternative is a schema whose
        # descriptions no longer say what the caller wrote.
        note_screening(manifest, "output_schema", "denied")
        raise InboundScreeningError("pii_blocked", status_code=422)


# Set on `RequestContext.extras` by an HTTP route that screened the turn before it built
# the agent — to answer 422 before a stream opens, or before a durable run is enqueued.
# The compiled agent then skips its own pass. Forgetting to set it costs a second screen
# (a second model call, when one is configured), never a hole.


__all__ = [
    "MAX_ARGUMENT_STRINGS",
    "MAX_SCHEMA_STRINGS",
    "screen_output_schema",
    "screen_tool_arguments",
    "screen_tool_output",
]
