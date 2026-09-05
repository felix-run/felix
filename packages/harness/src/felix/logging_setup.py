"""Structured logging and request correlation.

Three things were missing. `structlog` was a hard dependency that nothing imported;
`settings.log_level` was never applied, so the level was whatever the root logger
defaulted to; and there was no request or correlation id anywhere, which makes a
multi-tenant agent harness effectively undebuggable — a single chat request fans out
across tool calls, model calls, session writes, and audit events with nothing tying them
together.
"""

from __future__ import annotations

import logging
import uuid
from contextvars import ContextVar
from typing import Any

# Set per request by the API and read by the log processor. A ContextVar rather than a
# parameter because the agent loop and every wrapper below it log without any handle on
# the request.
_request_id: ContextVar[str] = ContextVar("felix_request_id", default="")

REQUEST_ID_HEADER = "x-request-id"


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]


def set_request_id(value: str) -> Any:
    return _request_id.set(value or new_request_id())


def reset_request_id(token: Any) -> None:
    _request_id.reset(token)


def get_request_id() -> str:
    return _request_id.get()


class LogIdsFilter(logging.Filter):
    """Attach the request, tenant and trace ids to every record, so plain `logging` calls
    carry them.

    The codebase logs through the stdlib everywhere. Rather than rewrite ~200 call sites
    to use structlog, the ids are injected here and rendered by the formatter. The tenant
    is what a multi-tenant operator filters by; the trace id is what joins a line to the
    span that produced it when OTel is on (and `-` when it is not).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # Both ids come from the caller — `x-request-id` is a header, the tenant may be a
        # JWT claim — and the text format is one line per record, so they are escaped at
        # this grammar boundary rather than trusted. The JSON format escapes anyway.
        record.request_id = loggable(_request_id.get() or "-", limit=64)
        record.tenant_id = loggable(_current_tenant(), limit=64)
        record.trace_id = _current_trace_id()
        return True


# Back-compat name; the filter has carried more than the request id since the tenant and
# trace ids joined it.
RequestIdFilter = LogIdsFilter


def _current_tenant() -> str:
    try:
        from felix.context import try_get_context

        ctx = try_get_context()
        return str(ctx.auth.tenant_id or "-") if ctx is not None else "-"
    except Exception:  # a filter that raises fails the caller's logging call, not the record
        return "-"


# Resolved once: `opentelemetry` is the `otel` extra, and a failed import is not cached by
# the import system, so retrying it on every record walked the finder chain each time.
_get_current_span: Any = None
_span_lookup_checked = False


def _current_trace_id() -> str:
    """The active OTel trace id as 32 hex digits, or `-` when no span is recording."""
    global _get_current_span, _span_lookup_checked
    if not _span_lookup_checked:
        _span_lookup_checked = True
        try:
            from opentelemetry import trace

            _get_current_span = trace.get_current_span
        except ImportError:
            _get_current_span = None
    if _get_current_span is None:
        return "-"
    try:
        context = _get_current_span().get_span_context()
    except Exception:  # same reason as `_current_tenant`
        return "-"
    if not context.is_valid:
        return "-"
    return format(context.trace_id, "032x")


def configure_logging(settings: Any) -> None:
    """Apply `FELIX_LOG_LEVEL` and install structured rendering.

    Idempotent: safe to call from both the API and the worker entrypoints.
    """
    level_name = str(getattr(settings, "log_level", "INFO") or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)

    # Replace our own handler rather than stacking one per call.
    for handler in list(root.handlers):
        if getattr(handler, "_felix", False):
            root.removeHandler(handler)

    handler = logging.StreamHandler()
    handler._felix = True  # type: ignore[attr-defined]
    handler.addFilter(LogIdsFilter())
    handler.setFormatter(_build_formatter(settings))
    root.addHandler(handler)


def _build_formatter(settings: Any) -> logging.Formatter:
    """`FELIX_LOG_FORMAT`: JSON so logs are queryable, text so a person can read them,
    `auto` picks JSON in production."""
    wanted = str(getattr(settings, "log_format", "auto") or "auto")
    if wanted == "auto":
        wanted = "json" if str(getattr(settings, "environment", "development")) == "production" else "text"
    if wanted == "json":
        return _json_formatter()
    return logging.Formatter(
        "%(asctime)s %(levelname)-7s [%(request_id)s %(tenant_id)s %(trace_id)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _json_formatter() -> logging.Formatter:
    import json

    class _Json(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            payload = {
                "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
                "level": record.levelname,
                "logger": record.name,
                "request_id": getattr(record, "request_id", "-"),
                "tenant_id": getattr(record, "tenant_id", "-"),
                "trace_id": getattr(record, "trace_id", "-"),
                "message": record.getMessage(),
            }
            if record.exc_info:
                payload["exception"] = self.formatException(record.exc_info)
            return json.dumps(payload, default=str)

    return _Json()


# Control characters, escaped rather than dropped. `\t`, `\n` and `\r` get their
# familiar spellings; everything else in the C0 range plus DEL becomes `\xNN`.
_LOG_ESCAPES: dict[int, str] = {c: f"\\x{c:02x}" for c in range(0x20)} | {
    0x09: "\\t",
    0x0A: "\\n",
    0x0D: "\\r",
    0x7F: "\\x7f",
}


def loggable(value: object, *, limit: int = 200) -> str:
    """Untrusted text, made safe to interpolate into a log line.

    A newline in a logged value forges a log entry. That is worth more than it sounds
    where the forged line can be a *refusal* or an error: an attacker who can write
    "auth failed for tenant X" into the log makes the trail argue for something that
    never happened, and the trail is what an incident is reconstructed from.

    Control characters are escaped rather than removed, so the value stays readable and
    a deliberate injection attempt is visible as `\\n` in the output instead of silently
    vanishing. Truncation is marked for the same reason -- a log line that was cut
    should not look like one that was short.

    `limit` is generous by default because the usual callers are gateway response
    bodies, where the content is the reason for logging at all. Pass something small
    for an identifier, where anything long is already not an identifier.
    """
    escaped = str(value).translate(_LOG_ESCAPES)
    if len(escaped) <= limit:
        return escaped or "<empty>"
    return escaped[:limit] + f"…(+{len(escaped) - limit})"


__all__ = [
    "REQUEST_ID_HEADER",
    "LogIdsFilter",
    "RequestIdFilter",
    "configure_logging",
    "get_request_id",
    "loggable",
    "new_request_id",
    "reset_request_id",
    "set_request_id",
]
