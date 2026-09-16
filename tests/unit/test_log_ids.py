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


def _at_column_zero(rendered: list[str]) -> list[str]:
    """The lines a log reader would treat as the start of a record."""
    return [line for line in rendered if line and not line.startswith(" ")]


def test_no_line_of_a_traceback_can_be_read_as_a_record() -> None:
    """The traceback is the half the message escape does not reach.

    `Formatter.format` appends `exc_text` *after* the message, and an exception's own
    `str` is not indented the way its frames are -- it renders at column 0. So a newline
    inside an exception message used to produce a fully record-shaped line, on any of the
    ~30 `exc_info=True` call sites whose exception text is built from a caller-influenced
    value. Escaping the block would have closed it by flattening the traceback to one
    line, which is unreadable and the reason it was left open.

    Indenting closes it without that cost: the property a forged record needs is the
    column, not the content. The forged text is still *there* -- nothing is dropped, and
    an operator can still read what the exception said -- it simply cannot begin a record.
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
    rendered = _build_formatter(_settings(log_format="text")).format(record).splitlines()

    # Still a readable traceback, not a flattened one.
    assert any("Traceback (most recent call last):" in line for line in rendered)
    assert len(rendered) > 3, "the traceback was flattened"

    # Exactly one line begins a record: the real one.
    assert _at_column_zero(rendered) == [rendered[0]]
    assert rendered[0].endswith("felix.test: it failed")

    # And the forged text survives, indented, rather than being silently dropped.
    assert forged not in rendered, "the forged line still starts at column 0"
    assert any(forged in line for line in rendered), "the exception text was lost, not contained"


def test_an_exotic_line_separator_in_an_exception_cannot_escape_the_indent() -> None:
    """`_indent` splits with `str.splitlines()` rather than `split("\n")` for this case.

    U+2028 ends a line for the same readers `_escape` covers it for, so splitting only on
    `\n` leaves everything after it inside one string -- indented at the front, and at
    column 0 from the separator onward for any viewer that honours it.

    Note there is no space after the separator: with one, the forged text would begin with
    a space whatever `_indent` did, and this test would pass against the bug it exists for.
    """
    import sys

    try:
        raise ValueError("boom\u20282026-09-04 INFO [x -] felix.auth: api key accepted")
    except ValueError:
        record = logging.LogRecord(
            "felix.test", logging.ERROR, __file__, 1, "it failed", None, sys.exc_info()
        )
    LogIdsFilter().filter(record)
    rendered = _build_formatter(_settings(log_format="text")).format(record).splitlines()

    assert _at_column_zero(rendered) == [rendered[0]]
    # Anchored, because "no line at column 0" is also true of a block that was dropped.
    assert rendered[0].endswith("felix.test: it failed")
    assert any("felix.auth: api key accepted" in line for line in rendered), "the text was lost"


def test_stack_info_is_indented_as_well_as_the_traceback() -> None:
    """`stack_info=True` is a second block `Formatter.format` appends after the message,
    through `formatStack` rather than `formatException`. It is rarer than `exc_info` and
    took the same treatment, because "rarer" is not a security property."""
    record = logging.LogRecord("felix.test", logging.ERROR, __file__, 1, "it failed", None, None)
    record.stack_info = "Stack (most recent call last):\n2026-09-04 INFO [x -] felix.auth: accepted"
    LogIdsFilter().filter(record)
    rendered = _build_formatter(_settings(log_format="text")).format(record).splitlines()

    assert _at_column_zero(rendered) == [rendered[0]]
    assert any("felix.auth: accepted" in line for line in rendered), "the stack text was lost"


def test_a_traceback_another_handler_already_rendered_is_not_trusted() -> None:
    """`Formatter.format` caches its work on `record.exc_text` and skips `formatException`
    entirely when it is already set.

    One record reaches every handler, so whichever formats first fills that cache -- and if
    it is a stock formatter, the block sitting there is unindented. Reusing it would undo
    the guarantee for exactly the multi-handler setup `tracing.py` creates, which is why
    `format()` clears it on the copy rather than inheriting it.
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

    # A different handler gets there first and leaves its unindented block on the record.
    logging.Formatter("%(message)s").format(record)
    assert record.exc_text and forged in record.exc_text.splitlines()

    before = record.exc_text
    rendered = _build_formatter(_settings(log_format="text")).format(record).splitlines()

    assert _at_column_zero(rendered) == [rendered[0]]
    assert rendered[0].endswith("felix.test: it failed")
    assert any(forged in line for line in rendered), "the block was dropped, not contained"
    # The direction production actually takes: `configure_logging` registers this handler
    # before `tracing.py` adds the OTel one, so this formatter is the one that runs *first*
    # and the one that could leave an indented block behind for the next handler to render.
    assert record.exc_text == before, "the indented block was pushed onto the shared record"


@pytest.mark.parametrize(
    ("payload", "escaped"),
    [
        # ESC [ 1 G is cursor-horizontal-absolute: a terminal redraws the line at column 0
        # however far right it was written, so indentation alone does not contain it.
        ("\x1b[1G2026-09-04 INFO felix.auth: accepted", "\\x1b[1G"),
        # The bidi override does not move the line, it garbles it in place.
        ("\u202e2026-09-04 INFO felix.auth: accepted", "\\u202e"),
    ],
)
def test_a_display_control_cannot_ride_into_a_traceback_unescaped(payload: str, escaped: str) -> None:
    """The traceback was the only text in a record that never met `_escape`.

    `_indent` splits and prefixes; `str.splitlines()` breaks on every Unicode *line*
    separator but on neither `\x1b` nor U+202E, so a test that asks only "does any line
    start at column 0" passes while a terminal still draws the forged text at column 0.
    The message path has escaped both since #241 -- this is the half that had not caught up.
    """
    import sys

    try:
        raise ValueError(f"boom{payload}")
    except ValueError:
        record = logging.LogRecord(
            "felix.test", logging.ERROR, __file__, 1, "it failed", None, sys.exc_info()
        )
    LogIdsFilter().filter(record)
    line = _build_formatter(_settings(log_format="text")).format(record)

    assert escaped in line, "the control character reached the traceback raw"
    assert payload[0] not in line, "a raw display control survived"
    assert _at_column_zero(line.splitlines()) == [line.splitlines()[0]]


def test_a_traceback_is_not_dropped_when_the_record_carries_text_but_no_exc_info() -> None:
    """`exc_text` set with `exc_info` cleared is a real record shape, not a contrivance.

    `Formatter.format` renders `exc_text` under its own `if`, *outside* the
    `if record.exc_info:` that would regenerate it -- so clearing the field to avoid
    inheriting an unindented block drops the traceback entirely, with no marker that
    anything was lost. `logging.handlers.SocketHandler.makePickle` builds exactly this
    shape on purpose ("just to get traceback text into record.exc_text", then
    `d['exc_info'] = None`), and a `QueueHandler.prepare` override may.

    Nothing in Felix creates it today, which is the point: the three tests that pin
    "no line begins a record" all pass on a dropped block, because a block that is gone
    trivially has no line at column 0. This one fails instead.
    """
    forged = "2026-09-04 INFO [x -] felix.auth: api key accepted for admin"
    record = logging.LogRecord("felix.test", logging.ERROR, __file__, 1, "it failed", None, None)
    record.exc_text = f'Traceback (most recent call last):\n  File "x.py", line 1\nValueError: boom\n{forged}'
    LogIdsFilter().filter(record)

    rendered = _build_formatter(_settings(log_format="text")).format(record).splitlines()

    assert any("ValueError: boom" in line for line in rendered), "the traceback was dropped"
    assert any(forged in line for line in rendered), "the exception text was lost"
    assert _at_column_zero(rendered) == [rendered[0]]
    assert record.exc_text.startswith("Traceback"), "the shared record was indented in place"


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
