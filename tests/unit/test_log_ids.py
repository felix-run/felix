"""Every log line carries the ids an operator filters by, in the format they asked for."""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from felix.config import Settings
from felix.context import AuthContext, RequestContext, run_with_context
from felix.logging_setup import LogIdsFilter, _build_formatter

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
