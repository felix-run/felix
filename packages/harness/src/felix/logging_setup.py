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


class RequestIdFilter(logging.Filter):
    """Attach the active request id to every record, so plain `logging` calls carry it.

    The codebase logs through the stdlib everywhere. Rather than rewrite ~200 call sites
    to use structlog, the id is injected here and rendered by the formatter.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _request_id.get() or "-"
        return True


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
    handler.addFilter(RequestIdFilter())
    handler.setFormatter(_build_formatter(settings))
    root.addHandler(handler)


def _build_formatter(settings: Any) -> logging.Formatter:
    """JSON in production so logs are queryable; readable text elsewhere."""
    if str(getattr(settings, "environment", "development")) == "production":
        try:
            return _json_formatter()
        except Exception:  # pragma: no cover - structlog always present via deps
            pass
    return logging.Formatter(
        "%(asctime)s %(levelname)-7s [%(request_id)s] %(name)s: %(message)s",
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
    "RequestIdFilter",
    "configure_logging",
    "get_request_id",
    "loggable",
    "new_request_id",
    "reset_request_id",
    "set_request_id",
]
