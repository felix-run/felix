"""GenAI span shaping — what a Felix span may say about a model call.

Lifted out of `patterns/model.py`, which had grown to a third telemetry and was over the
module budget. None of this is routing or metering, which is that module's subject: these
answer "what may a span say", and the answers are the same whether the call came from the
react loop, a delegating pattern or a plugin.

Attribute names follow the OpenTelemetry **GenAI semantic conventions** wherever one
exists, so a backend that speaks them renders a model call as a generation with token usage
attached rather than as an anonymous timing bar. Borrowed rather than invented: that choice
is the reason Memoturn, Jaeger and Grafana all read these spans with no Felix-specific code.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from felix_ai.types import ChatMessage, ModelChatResult

from felix.config import get_settings
from felix.context import try_get_context
from felix.observability.tracing import SpanContext

logger = logging.getLogger("felix.observability.genai")


# Prompts grow without bound; a span attribute should not. Truncation is preferable to a
# span the exporter refuses or the backend rejects.
MAX_SPAN_CONTENT_CHARS = 32_000


def _content_capture_enabled() -> bool:
    """`FELIX_OTEL_CAPTURE_CONTENT`, read from the active request's settings.

    Off by default. A tracing backend is an egress destination like any other, and span
    attributes do not pass through the governance content screening that guards tool
    output — so the prompt and the completion only leave the process when an operator has
    said so.
    """
    ctx = try_get_context()
    settings = getattr(ctx, "settings", None) if ctx is not None else None
    if settings is None:
        settings = get_settings()
    return bool(getattr(settings, "otel_capture_content", False))


def _redacted_json(value: Any) -> str | None:
    """Serialise for a span attribute, with the audit store's redaction applied first.

    `redact_json` is what the audit path already uses, so content on a span is masked to
    the same standard as content in an audit row — and to no better one. It replaces
    values Felix *knows* are secrets (those hydrated from the secrets backend) by
    substring match; it is not pattern detection. A credential a user types into a chat is
    therefore exported verbatim. That boundary is why `FELIX_OTEL_CAPTURE_CONTENT`
    defaults to off.
    """
    try:
        from felix.secrets import redact_json

        return json.dumps(redact_json(value), default=str)[:MAX_SPAN_CONTENT_CHARS]
    except Exception:
        logger.debug("span content redaction failed; dropping content", exc_info=True)
        return None


def record_input_on_span(span: SpanContext, messages: list[ChatMessage]) -> None:
    if not _content_capture_enabled():
        return
    payload = _redacted_json([_message_dict(m) for m in messages])
    if payload is not None:
        span.set_attribute("gen_ai.input.messages", payload)


def record_output_on_span(span: SpanContext, result: ModelChatResult) -> None:
    if not _content_capture_enabled():
        return
    payload = _redacted_json([_message_dict(result.message)])
    if payload is not None:
        span.set_attribute("gen_ai.output.messages", payload)


def _message_dict(message: Any) -> dict[str, Any]:
    """The parts of a message worth tracing, without dragging in provider internals."""
    content = getattr(message, "content", None)
    out: dict[str, Any] = {"role": getattr(message, "role", "") or "", "content": content}
    calls = getattr(message, "tool_calls", None)
    if calls:
        out["tool_calls"] = [
            {"name": getattr(c, "name", ""), "args": getattr(c, "args", None)} for c in calls
        ]
    return out


def record_result_on_span(span: SpanContext, result: ModelChatResult, wire_model: str) -> None:
    """Attach the response half of a generation: stop reason, tokens, cost."""
    span.set_attribute("gen_ai.response.model", wire_model)
    span.set_attribute("gen_ai.response.finish_reasons", str(result.stop_reason))
    record_output_on_span(span, result)
    usage = result.usage
    if usage is None:
        # Same failure `_metered_usage` warns about — a turn that cannot be capped.
        # Worth seeing in a trace, not only in the log.
        span.set_attribute("felix.usage.unmetered", True)
        return
    span.set_attribute("gen_ai.usage.input_tokens", usage.input)
    span.set_attribute("gen_ai.usage.output_tokens", usage.output)
    # Cache tokens under the GenAI-semconv names as well as Felix's own. Only the former
    # are read by backends, and dropping them is not cosmetic: Felix's `usage_with_cost`
    # counts cache reads and creations, so a backend computing cost from input/output
    # alone silently disagrees with Felix's own number as soon as prompt caching is on.
    span.set_attribute("gen_ai.usage.cache_creation_input_tokens", usage.cache_creation)
    span.set_attribute("gen_ai.usage.cache_read_input_tokens", usage.cache_read)
    span.set_attribute("felix.usage.cache_creation", usage.cache_creation)
    span.set_attribute("felix.usage.cache_read", usage.cache_read)
    try:
        from felix.usage.pricing import usage_with_cost

        priced = usage_with_cost(usage, model_id=wire_model)
        span.set_attribute("felix.cost_usd", float((priced.get("cost") or {}).get("total") or 0.0))
    except Exception:
        logger.debug("span pricing unavailable", exc_info=True)


__all__ = [
    "MAX_SPAN_CONTENT_CHARS",
    "record_input_on_span",
    "record_output_on_span",
    "record_result_on_span",
]
