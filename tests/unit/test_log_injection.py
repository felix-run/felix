"""Caller-supplied text that reaches a log line cannot forge one.

CodeQL's `py/log-injection` flagged three sites where a value a caller controls was logged bare: a
`/v1` content part's `type` (`felix_ai.types`), and a skill library object key and file path
(`felix.skills.library`). These drive the real code paths and read the records they write, so a
regression to logging the raw value fails here rather than at the next scan.
"""

from __future__ import annotations

import logging

import pytest
from felix_ai.types import ChatMessage, _loggable

FORGED = "x\n2026-10-04 INFO felix.auth: admin login ok"


def test_an_unrecognised_part_type_cannot_start_a_log_line(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="felix_ai.types")
    ChatMessage.model_validate(
        {"role": "user", "content": [{"type": FORGED}, {"type": "text", "text": "hi"}]}
    )
    (record,) = [r for r in caplog.records if "unrecognised type" in r.getMessage()]
    message = record.getMessage()
    assert "\n" not in message
    assert "x\\n2026-10-04" in message  # escaped, so the attempt stays visible


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("input_audio", "input_audio"),
        ("a\rb", "a\\rb"),
        ("line sep", "line\\u2028sep"),  # a line break no C0 table covers
        ("rtl‮x", "rtl\\u202ex"),  # a bidi override reorders a line without a newline
    ],
)
def test_the_escape_keeps_identifiers_and_neutralises_line_breaks(raw: str, expected: str) -> None:
    assert _loggable(raw) == expected


def test_the_escape_marks_what_it_cut() -> None:
    assert _loggable("t" * 100, limit=10) == "tttttttttt…(+90)"


def test_a_part_type_list_is_bounded() -> None:
    """A message carrying thousands of distinct junk types logs twenty, not thousands."""
    content = [{"type": f"junk{i}"} for i in range(500)]
    logger = logging.getLogger("felix_ai.types")
    seen: list[str] = []

    class _Grab(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            seen.append(record.getMessage())

    handler = _Grab(level=logging.WARNING)
    logger.addHandler(handler)
    try:
        ChatMessage.model_validate({"role": "user", "content": content})
    finally:
        logger.removeHandler(handler)
    (message,) = [m for m in seen if "unrecognised type" in m]
    assert message.startswith("dropping 500 content part(s)")
    assert message.count("junk") == 20


class _BrokenStore:
    """An object store that fails every call, so each probe and cleanup takes its logging branch."""

    async def exists(self, key: str) -> bool:
        raise OSError("store down")

    async def delete(self, key: str) -> None:
        raise OSError("store down")


def _single_line(caplog: pytest.LogCaptureFixture, needle: str) -> list[str]:
    messages = [r.getMessage() for r in caplog.records if needle in r.getMessage()]
    assert messages, f"no {needle!r} record was written"
    for message in messages:
        assert "\n" not in message, message
    return messages


async def test_a_skill_name_cannot_forge_a_line_when_the_store_probe_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A skill name is caller-supplied and lands in the object key the probe logs."""
    from felix.config import Settings
    from felix.skills import library

    caplog.set_level(logging.WARNING, logger="felix.skills.library")
    owned = await library.host_owns(
        Settings(database_url="memory://log-injection"), "acme", FORGED, object_store=_BrokenStore()
    )
    assert owned is False
    messages = _single_line(caplog, "object store probe failed")
    assert any("x\\n2026-10-04" in m for m in messages)


async def test_a_file_path_cannot_forge_a_line_when_cleanup_fails(caplog: pytest.LogCaptureFixture) -> None:
    """A failed save's file paths are the caller's, and cleanup logs each one it cannot remove."""
    from felix.skills import library

    class _Lib:
        async def delete_draft(self, tenant_id: str, name: str, version: str) -> None:
            return None

    caplog.set_level(logging.WARNING, logger="felix.skills.library")
    await library._discard(
        _Lib(), _BrokenStore(), "acme", {"name": "s", "version": "1.0.0"}, {FORGED: "body"}
    )
    _single_line(caplog, "of a failed save")
