"""Structured logging and request correlation.

Three things were missing. `structlog` was a hard dependency that nothing imported;
`settings.log_level` was never applied, so the level was whatever the root logger
defaulted to; and there was no request or correlation id anywhere, which makes a
multi-tenant agent harness effectively undebuggable — a single chat request fans out
across tool calls, model calls, session writes, and audit events with nothing tying them
together.
"""

from __future__ import annotations

import copy
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


# Control characters, escaped rather than dropped. `\t`, `\n` and `\r` get their
# familiar spellings; everything else in the C0 range plus DEL becomes `\xNN`.
_LOG_ESCAPES: dict[int, str] = {c: f"\\x{c:02x}" for c in range(0x20)} | {
    0x09: "\\t",
    0x0A: "\\n",
    0x0D: "\\r",
    0x7F: "\\x7f",
}


def _escape(text: str) -> str:
    """`text` with every character that could end a log line replaced by its escape.

    Two passes: the C0 range is the common case and worth one C-level `translate`, and
    `str.isprintable()` then names exactly what the table cannot cover -- U+0085, U+2028
    and U+2029, which end a line for a reader the table never considered, and the bidi
    overrides, which reorder a record's visible text without changing a byte of it. The
    per-character pass runs only when that check says something was left behind.
    """
    escaped = text.translate(_LOG_ESCAPES)
    if escaped.isprintable():
        return escaped
    return "".join(ch if ch.isprintable() else ch.encode("unicode_escape").decode("ascii") for ch in escaped)


def _indent(block: str) -> str:
    """Every line of `block` pushed off column 0, so none of it can begin a record.

    `deploy/GOVERNANCE.md` has the why. Two things to know here.

    The splitter is `str.splitlines()` rather than `split("\\n")`, because it also breaks on
    U+2028, U+2029, U+0085 and `\\v`/`\\f`/`\\x1c`-`\\x1e`, which would otherwise step back out
    to column 0. They are normalised to `\\n` in the process, so a block that went through
    here no longer shows *which* separator it carried -- evidence a log message keeps and a
    traceback does not, and the reason this is used on tracebacks and nothing else.

    Each line is then escaped, because indentation alone is a claim about columns and a
    terminal does not have to honour it: `\\x1b[1G` is cursor-horizontal-absolute, so an ESC
    surviving into a traceback redraws that line at column 0 no matter how far right it was
    written, and the bidi overrides reorder it in place. `splitlines()` has already removed
    every break character by this point, so escaping here cannot flatten anything -- the only
    visible cost is that a tab inside a frame's source line renders as `\\t`.
    """
    return "\n".join("  " + _escape(line) for line in block.splitlines())


def loggable(value: object, *, limit: int = 200) -> str:
    """Untrusted text, made safe to interpolate into a log line. See `deploy/GOVERNANCE.md`.

    Control characters are escaped rather than removed, so a deliberate injection attempt
    stays visible as `\\n` instead of silently vanishing, and truncation is marked for the
    same reason -- a line that was cut should not look like one that was short.

    `limit` is generous by default because the usual callers are gateway response bodies,
    where the content is the reason for logging at all. Pass something small for an
    identifier, where anything long is already not an identifier.

    `_TextFormatter` escapes too, so this is no longer the only thing standing between a
    newline and a forged record -- but the bound lives here and nowhere else, because a
    formatter sees a finished record and truncating there would cut the record.
    """
    escaped = _escape(str(value))
    if len(escaped) <= limit:
        return escaped or "<empty>"
    return escaped[:limit] + f"…(+{len(escaped) - limit})"


class _TextFormatter(logging.Formatter):
    """Text output, with the caller's message escaped before it is rendered.

    A newline reaching `%(message)s` splits one record into two, and the second is
    attacker-written; `deploy/GOVERNANCE.md` has the threat model. Escaping here rather
    than at each call site is what makes that structural instead of a list to maintain.

    Four decisions a maintainer cannot recover from the code:

    * **Not a `logging.Filter`,** though the docs invite mutating records there and one
      filter would cover every handler. A record is shared by all of them, so escaping
      before the format is chosen corrupts a JSON handler's output to fix a text
      handler's bug -- and JSON was never exposed, since `json.dumps` escapes the
      separator for a value. The `copy.copy` is that argument at smaller scale: `format()`
      may not alter a record the next handler has yet to read. `tracing.py` really does
      attach a second handler to the root logger, so this is not hypothetical.
    * **Not `formatMessage()`,** which is shorter and is what CPython's own `format()`
      calls, but appears in no version of the logging docs. Only documented API is used
      here: `LogRecord.getMessage()`, the `msg`/`args` attributes, and `Formatter.format()`.
    * **`args` is cleared** because the copy already carries the merged message, and
      leaving them would re-apply `%` to attacker-influenced text.
    * **Tracebacks are indented rather than escaped**, so they stay multi-line and
      readable while still being unable to forge a record. `Formatter.format` appends
      `exc_text` *after* the message, and an exception's own `str` is not indented the way
      its frames are -- it renders at column 0, so a newline inside an exception message
      produced a record-shaped line, on any of the ~30 `exc_info=True` call sites whose
      exception text is built from a caller-influenced value. Escaping the block would
      flatten the traceback to one line; pushing every line off column 0 keeps the shape
      an operator reads and removes the one property a forged record needs.

    Three costs, all accepted: `exc_text` is recomputed rather than shared, so a record
    carrying an exception formats its traceback once per handler; frame lines sit two
    columns further right than a stock traceback; and a deliberately multi-line *message*
    is flattened with no opt-out -- nothing logs one today, but a future
    `logger.debug("compiled:\\n%s", yaml)` will not render as its author expects.
    """

    def format(self, record: logging.LogRecord) -> str:
        safe = copy.copy(record)
        safe.msg = _escape(record.getMessage())
        safe.args = None
        # A cache another formatter filled is indented here rather than recomputed, because
        # `Formatter.format` renders `exc_text` under its own `if`, *outside* the
        # `if record.exc_info:` that would refill it. Clearing it therefore drops the
        # traceback silently whenever a record arrives with the text but not the tuple --
        # which `logging.handlers.SocketHandler.makePickle` constructs deliberately, and any
        # `QueueHandler.prepare` override may. Indenting covers that record and the ordinary
        # one with the same line, and cannot double-indent: this writes only to the copy, so
        # a block on the original can only have come from some other formatter.
        safe.exc_text = _indent(record.exc_text) if record.exc_text else None
        return super().format(safe)

    def formatException(self, ei: Any) -> str:
        return _indent(super().formatException(ei))

    def formatStack(self, stack_info: str) -> str:
        return _indent(super().formatStack(stack_info))


class _JsonFormatter(logging.Formatter):
    """One JSON object per record, so logs are queryable.

    Never had the text format's injection problem: the message is a *value* here, and
    `json.dumps` escapes the separator on its way in. Escaping again would double every
    backslash an operator reads.
    """

    def format(self, record: logging.LogRecord) -> str:
        import json

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


def _build_formatter(settings: Any) -> logging.Formatter:
    """`FELIX_LOG_FORMAT`: JSON so logs are queryable, text so a person can read them,
    `auto` picks JSON in production."""
    wanted = str(getattr(settings, "log_format", "auto") or "auto")
    if wanted == "auto":
        wanted = "json" if str(getattr(settings, "environment", "development")) == "production" else "text"
    if wanted == "json":
        return _JsonFormatter()
    return _TextFormatter(
        "%(asctime)s %(levelname)-7s [%(request_id)s %(tenant_id)s %(trace_id)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


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
