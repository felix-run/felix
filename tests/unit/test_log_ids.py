"""Every log line carries the ids an operator filters by, in the format they asked for."""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from felix.config import Settings
from felix.context import AuthContext, RequestContext, run_with_context
from felix.logging_setup import LogIdsFilter, _build_formatter, loggable

from tests.optional_deps import require_optional


def _record(msg: str = "hello") -> logging.LogRecord:
    record = logging.LogRecord("felix.test", logging.INFO, __file__, 1, msg, None, None)
    LogIdsFilter().filter(record)
    return record


def _settings(**kw: Any) -> Settings:
    return Settings(database_url="memory://logs", object_store="memory", allow_insecure=True, **kw)


def test_records_carry_the_tenant_of_the_active_request() -> None:
    ctx = RequestContext(settings=_settings(), auth=AuthContext(tenant_id="acme"))
    with run_with_context(ctx):
        record = _record()
    assert record.tenant_id == "acme"  # type: ignore[attr-defined]
    assert _record().tenant_id == "-"  # type: ignore[attr-defined]


def test_records_carry_a_trace_id_only_while_a_span_records() -> None:
    assert _record().trace_id == "-"  # type: ignore[attr-defined]
    otel = require_optional("opentelemetry.sdk.trace", "otel")
    provider = otel.TracerProvider()
    with provider.get_tracer("t").start_as_current_span("s") as span:
        record = _record()
        expected = format(span.get_span_context().trace_id, "032x")
    assert record.trace_id == expected  # type: ignore[attr-defined]
    assert len(record.trace_id) == 32  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("log_format", "environment", "json_expected"),
    [
        ("auto", "production", True),
        ("auto", "development", False),
        ("json", "development", True),
        ("text", "production", False),
    ],
)
def test_log_format_is_a_setting_with_auto_following_the_environment(
    log_format: str, environment: str, json_expected: bool
) -> None:
    formatter = _build_formatter(_settings(log_format=log_format, environment=environment))
    line = formatter.format(_record("hi"))
    if json_expected:
        payload = json.loads(line)
        assert payload["message"] == "hi"
        assert {"request_id", "tenant_id", "trace_id"} <= set(payload)
    else:
        assert "hi" in line and not line.startswith("{")


def test_the_text_format_shows_the_tenant() -> None:
    ctx = RequestContext(settings=_settings(), auth=AuthContext(tenant_id="acme"))
    formatter = _build_formatter(_settings(log_format="text"))
    with run_with_context(ctx):
        line = formatter.format(_record("hi"))
    assert "acme" in line


def test_a_newline_in_a_tenant_id_cannot_forge_a_log_line() -> None:
    """`assert_valid_tenant_id` validates the thread-id grammar, not the log-line one: a
    claim-mode JWT tenant with a newline in it passed, and the text format is one line
    per record. Escaped at the boundary, so a forged second line stays inside the first."""
    forged = "acme\n2026-09-04 INFO [x -] felix.auth: api key accepted for admin"
    ctx = RequestContext(settings=_settings(), auth=AuthContext(tenant_id=forged))
    formatter = _build_formatter(_settings(log_format="text"))
    with run_with_context(ctx):
        line = formatter.format(_record("real message"))
    assert "\n" not in line and "acme\\n2026" in line


# Driven through `loggable` *and* through the formatter below. Keeping one table is the
# point: the guarantee moved from the helper to the formatter, so a table that covered
# only the helper would let the formatter quietly regress to the C0 range alone.
_LINE_BREAK_SHAPES = [
    ("plain/quick v2", "plain/quick v2"),
    ("a\nb", "a\\nb"),
    ("a\rb", "a\\rb"),
    ("a\tb", "a\\tb"),
    ("a\x00b", "a\\x00b"),
    # Above DEL, where a `translate` table over the C0 range sees nothing. Each of
    # these ends a line or reorders one for *some* reader, which is the whole point:
    # the escaping has to cover every grammar the record is read in, not just the
    # one `tail` uses.
    ("a\u2028b", "a\\u2028b"),  # LINE SEPARATOR: a new line in a JS log viewer
    ("a\u2029b", "a\\u2029b"),  # PARAGRAPH SEPARATOR, same
    ("a\x85b", "a\\x85b"),  # NEL
    ("a\u202eb", "a\\u202eb"),  # RTL OVERRIDE: reorders the visible record
]


@pytest.mark.parametrize(("raw", "expected"), _LINE_BREAK_SHAPES)
def test_loggable_escapes_every_shape_of_line_break(raw: str, expected: str) -> None:
    """The stricter of the two helpers that used to do this job. The one being removed
    covered the non-ASCII half and this one did not, so the collapse took the stricter
    behaviour -- a merge that quietly dropped U+2028 would have been a regression wearing
    a refactor's clothes."""
    assert loggable(raw) == expected


@pytest.mark.parametrize(("raw", "expected"), _LINE_BREAK_SHAPES)
def test_the_formatter_escapes_every_shape_too(raw: str, expected: str) -> None:
    """The same table through the production path, because this is where the guarantee
    now lives.

    Without this, `safe.msg = record.getMessage().translate(_LOG_ESCAPES)` -- the C0 table
    alone, dropping the Unicode pass -- leaves every other test in this file green: they
    all use a plain `\n`, which the table already covers. That is exactly the regression
    shape the helper's own table was written to catch, unguarded on the side that matters.
    """
    record = logging.LogRecord("felix.test", logging.INFO, __file__, 1, "stored %s", (raw,), None)
    LogIdsFilter().filter(record)
    line = _build_formatter(_settings(log_format="text")).format(record)
    assert f"stored {expected}" in line
    assert len(line.splitlines()) == 1, "the record was split in two"


def test_a_raw_newline_in_the_message_cannot_forge_a_line_either() -> None:
    """The call site that nobody wrapped.

    `LogIdsFilter` has routed `tenant_id` through `loggable` for a while, so the *ids*
    were safe; the message was not, and four separate findings fixed four separate
    call sites without ever making the fifth impossible. This asserts the structural
    property instead: whatever reaches `%(message)s`, one record is one line.
    """
    # The `%s` and `%(tenant_id)s` are the second half of this: `format()` clears `args`
    # on its copy, so the merged message must render literally. Leave them on and the
    # escaped text is interpolated a second time -- against attacker-influenced input.
    forged = "acme\n2026-09-04 INFO [x -] felix.auth: api key %s accepted for %(tenant_id)s"
    record = logging.LogRecord(
        "felix.test", logging.INFO, __file__, 1, "stored manifest %s failed", (forged,), None
    )
    LogIdsFilter().filter(record)
    line = _build_formatter(_settings(log_format="text")).format(record)
    assert "\n" not in line
    assert "acme\\n2026" in line, "the newline should be shown, not silently stripped"
    assert "%s accepted for %(tenant_id)s" in line, "the message was interpolated twice"


def test_json_logging_never_had_this_problem() -> None:
    """Why the fix belongs in the text formatter and only there.

    `json.dumps` escapes the separator because the message is a *value* in an object,
    not a line in a file -- so under `FELIX_LOG_FORMAT=json` a newline is already inert
    and re-escaping it here would double up every backslash an operator reads.
    """
    record = logging.LogRecord("felix.test", logging.INFO, __file__, 1, "stored %s", ("acme\nforged",), None)
    LogIdsFilter().filter(record)
    line = _build_formatter(_settings(log_format="json")).format(record)
    assert "\n" not in line
    assert json.loads(line)["message"] == "stored acme\nforged", "the value should survive intact"


def test_a_traceback_stays_multi_line_and_the_hole_that_leaves_is_pinned() -> None:
    """The escape goes on the message rather than the rendered record so tracebacks stay
    readable -- and this records exactly what that costs, because the cost is not nothing.

    `Formatter.format` appends `exc_text` after the escaped message, and an exception's
    own `str` is *not* indented the way its frames are: it lands at column 0. So a newline
    inside an exception message still produces a record-shaped line, and roughly thirty
    `exc_info=True` call sites can carry caller-influenced text into one.

    This is not a regression -- stock `logging.Formatter` has always appended `exc_text`,
    and nothing about this change made it worse. It is asserted rather than left implied
    so that "one record is one line" is read with the boundary attached, and so that a
    future change closing it (indenting `exc_text` would) fails here and gets noticed.
    """
    import sys

    forged = "2026-09-04 INFO [x -] felix.auth: api key accepted for admin"
    try:
        raise ValueError(f"boom\n{forged}")
    except ValueError:
        record = logging.LogRecord(
            "felix.test", logging.ERROR, __file__, 1, "it failed", None, sys.exc_info()
        )
    LogIdsFilter().filter(record)
    line = _build_formatter(_settings(log_format="text")).format(record)
    rendered = line.splitlines()

    assert "Traceback (most recent call last):" in line
    assert len(rendered) > 3, "the traceback was flattened"
    # The message this formatter is responsible for is still exactly one line.
    assert rendered[0].endswith("felix.test: it failed")
    # And the part it is not responsible for: still forgeable, at column 0.
    assert forged in rendered, "the exemption changed shape -- re-read the docstring"


def test_formatting_as_text_does_not_corrupt_the_same_record_as_json() -> None:
    """One record is shared by every handler attached to the logger.

    So the text formatter escapes a *copy*: mutating `record.msg` in place would be a
    text-format concern rewriting what a JSON handler -- or an operator's own third
    handler -- has yet to read. This is the reason the escape is not a `logging.Filter`
    either, where the docs invite exactly that mutation.
    """
    record = logging.LogRecord("felix.test", logging.INFO, __file__, 1, "stored %s", ("acme\nforged",), None)
    LogIdsFilter().filter(record)

    text = _build_formatter(_settings(log_format="text")).format(record)
    payload = json.loads(_build_formatter(_settings(log_format="json")).format(record))

    assert "\n" not in text
    assert payload["message"] == "stored acme\nforged", "the text formatter mutated the shared record"


def test_text_that_cannot_be_encoded_does_not_take_down_the_logging_call() -> None:
    """`_escape` falls back to `encode("unicode_escape")`, which is a codec and can refuse.

    It does not refuse on lone surrogates, and that matters: `os.fsdecode` of an invalid
    filename yields exactly that shape, and a path is a plausible thing to log. If it ever
    did refuse, the exception would surface at the `logger.warning(...)` call site rather
    than here -- a formatter that raises loses the record and the caller with it.
    """
    surrogate = b"bad\xffname".decode("utf-8", "surrogateescape")
    record = logging.LogRecord("felix.test", logging.INFO, __file__, 1, "read %s", (surrogate,), None)
    LogIdsFilter().filter(record)

    line = _build_formatter(_settings(log_format="text")).format(record)
    assert "\\udcff" in line and len(line.splitlines()) == 1


def test_a_failing_id_lookup_does_not_fail_the_logging_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """A handler filter that raises propagates to the `logger.info(...)` call site — inside
    the governance error paths that log from `except` blocks. The ids degrade to `-`."""
    from felix import context, logging_setup

    def boom() -> Any:
        raise RuntimeError("context broke")

    monkeypatch.setattr(context, "try_get_context", boom)
    monkeypatch.setattr(logging_setup, "_get_current_span", boom)
    monkeypatch.setattr(logging_setup, "_span_lookup_checked", True)
    record = _record()
    assert (record.tenant_id, record.trace_id) == ("-", "-")  # type: ignore[attr-defined]
