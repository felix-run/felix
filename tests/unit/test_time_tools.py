"""The time tools: `time_now`, `time_convert`, `date_math`.

Each answer is checked as text, through the tool the production provider builds
(`default_tool_provider`), so a zone left out reads `FELIX_TIMEZONE` the way a run does. The
daylight-saving cases are the reason these exist: a model converting 01:30 on the first Sunday of
November in New York gets one of two answers, and the tool has to say there are two.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest
from felix.tools import time_tools as T
from felix.tools.builtins import default_tool_provider
from felix.tools.errors import ToolErrorCode, read_tool_error_code
from felix.tools.types import output_text


async def _call(name: str, args: dict[str, Any]) -> Any:
    [tool] = default_tool_provider().resolve([name])
    return await tool.executor.execute(args)


@pytest.fixture(autouse=True)
def _phoenix(monkeypatch: pytest.MonkeyPatch) -> None:
    # UTC-7 all year, so the default zone is visible in the offset.
    monkeypatch.setenv("FELIX_TIMEZONE", "America/Phoenix")


async def test_the_tools_are_local_read_only_and_bound_by_name() -> None:
    tools = default_tool_provider().resolve(list(T.TOOL_NAMES))
    assert [t.name for t in tools] == list(T.TOOL_NAMES)
    for tool in tools:
        assert tool.executor.transport == "local"
        assert tool.read_only and tool.replay_safe


async def test_time_now_defaults_to_the_install_zone() -> None:
    out = output_text(await _call("time_now", {}))
    assert "· America/Phoenix · UTC-07:00 (MST)" in out


def test_time_now_names_each_zone_asked() -> None:
    moment = datetime(2026, 10, 30, 13, 34, tzinfo=UTC)
    out = T.time_now(T.TimeNowArgs(zones=["Asia/Tokyo", "Europe/London"]), default_zone="UTC", now=moment)
    assert out.splitlines() == [
        "2026-10-30 22:34 Friday · Asia/Tokyo · UTC+09:00 (JST)",
        "2026-10-30 13:34 Friday · Europe/London · UTC+00:00 (GMT)",
    ]


async def test_convert_reads_a_wall_time_in_from_zone() -> None:
    out = output_text(
        await _call(
            "time_convert",
            {"time": "2026-11-14T09:00", "from_zone": "America/New_York", "to_zones": ["Asia/Tokyo"]},
        )
    )
    assert out.splitlines() == [
        "from: 2026-11-14 09:00 Saturday · America/New_York · UTC-05:00 (EST)",
        "- 2026-11-14 23:00 Saturday · Asia/Tokyo · UTC+09:00 (JST)",
    ]


async def test_convert_without_from_zone_uses_the_install_zone() -> None:
    out = output_text(await _call("time_convert", {"time": "2026-11-14T09:00", "to_zones": ["UTC"]}))
    assert out.splitlines()[-1] == "- 2026-11-14 16:00 Saturday · UTC · UTC+00:00"


def test_convert_takes_an_offset_as_the_moment() -> None:
    args = T.TimeConvertArgs(time="2026-11-14T09:00+02:00", from_zone="Asia/Tokyo", to_zones=["UTC"])
    assert (
        T.time_convert(args, default_zone="UTC").splitlines()[-1]
        == "- 2026-11-14 07:00 Saturday · UTC · UTC+00:00"
    )


def test_a_repeated_hour_is_named_with_both_readings() -> None:
    args = T.TimeConvertArgs(time="2026-11-01T01:30", from_zone="America/New_York", to_zones=["UTC"])
    lines = T.time_convert(args, default_zone="UTC").splitlines()
    assert lines[0] == "from: 2026-11-01 01:30 Sunday · America/New_York · UTC-04:00 (EDT)"
    assert lines[1] == (
        "note: 2026-11-01 01:30 happens twice in this zone (clocks go back); "
        "the first was used, the second is UTC-05:00"
    )
    assert lines[2] == "- 2026-11-01 05:30 Sunday · UTC · UTC+00:00"


def test_a_skipped_hour_is_moved_past_the_gap_and_said() -> None:
    args = T.TimeConvertArgs(time="2026-03-08T02:30", from_zone="America/New_York", to_zones=["UTC"])
    lines = T.time_convert(args, default_zone="UTC").splitlines()
    assert lines[0] == "from: 2026-03-08 03:30 Sunday · America/New_York · UTC-04:00 (EDT)"
    assert lines[1] == "note: 2026-03-08 02:30 does not exist in this zone (clocks go forward); read as 03:30"
    assert lines[2] == "- 2026-03-08 07:30 Sunday · UTC · UTC+00:00"


@pytest.mark.parametrize("bad", ["../../etc/passwd", "/etc/localtime", "Mars/Olympus_Mons", "a.b", ""])
async def test_a_zone_that_is_not_an_iana_name_is_refused(bad: str) -> None:
    out = await _call("time_now", {"zones": [bad]})
    assert read_tool_error_code(out) is ToolErrorCode.INVALID_ARGUMENTS
    assert "time zone" in output_text(out)


async def test_a_date_without_a_time_cannot_be_converted() -> None:
    out = await _call("time_convert", {"time": "2026-11-14", "to_zones": ["UTC"]})
    assert read_tool_error_code(out) is ToolErrorCode.INVALID_ARGUMENTS


@pytest.mark.parametrize(
    ("start", "months", "landed"),
    [
        (date(2027, 1, 31), 1, date(2027, 2, 28)),
        (date(2028, 1, 31), 1, date(2028, 2, 29)),
        (date(2026, 3, 31), -1, date(2026, 2, 28)),
        (date(2026, 11, 15), 14, date(2028, 1, 15)),
        (date(2026, 1, 15), -13, date(2024, 12, 15)),
    ],
)
def test_months_clamp_to_the_last_day(start: date, months: int, landed: date) -> None:
    assert T.add_months(start, months) == landed


async def test_add_to_a_date() -> None:
    out = output_text(
        await _call("date_math", {"start": "2027-01-31", "op": "add", "add": {"months": 1, "days": 1}})
    )
    assert out == "result: 2027-03-01 Monday"


def test_a_week_later_keeps_the_wall_clock_across_a_dst_change() -> None:
    args = T.DateMathArgs(start="2026-10-29T09:00", op="add", add=T.Shift(weeks=1), zone="America/New_York")
    assert T.date_math(args, default_zone="UTC") == (
        "result: 2026-11-05 09:00 Thursday · America/New_York · UTC-05:00 (EST)"
    )


def test_hours_are_elapsed_time_across_a_dst_change() -> None:
    # Midnight plus three hours on the night 01:00-02:00 happens twice is 02:00, not 03:00.
    args = T.DateMathArgs(start="2026-11-01T00:00", op="add", add=T.Shift(hours=3), zone="America/New_York")
    assert T.date_math(args, default_zone="UTC") == (
        "result: 2026-11-01 02:00 Sunday · America/New_York · UTC-05:00 (EST)"
    )


async def test_between_two_dates_counts_days_and_weekdays() -> None:
    out = output_text(await _call("date_math", {"start": "2026-10-30", "op": "between", "end": "2026-11-14"}))
    assert out.splitlines() == [
        "days: 15 (end is 2 weeks and 1 day after start)",
        "calendar: end is 15 days after start",
        "weekdays (Mon-Fri, counting 2026-10-30, not 2026-11-14): 11",
        "2026-10-30 is a Friday; 2026-11-14 is a Saturday",
    ]


def test_between_reads_backwards_as_negative() -> None:
    args = T.DateMathArgs(start="2026-03-01", op="between", end="2024-02-29")
    lines = T.date_math(args, default_zone="UTC").splitlines()
    assert lines[0] == "days: -731 (end is 104 weeks and 3 days before start)"
    assert lines[1] == "calendar: end is 2 years and 1 day before start"


@pytest.mark.parametrize(
    ("start", "end", "weekdays"),
    [("2026-11-02", "2026-11-09", 5), ("2026-11-07", "2026-11-09", 0), ("2026-11-06", "2026-11-07", 1)],
)
def test_weekdays_between(start: str, end: str, weekdays: int) -> None:
    assert T.weekdays_between(date.fromisoformat(start), date.fromisoformat(end)) == weekdays


def test_between_two_date_times_is_elapsed_time() -> None:
    args = T.DateMathArgs(
        start="2026-11-01T00:00", op="between", end="2026-11-01T06:00", zone="America/New_York"
    )
    assert T.date_math(args, default_zone="UTC").splitlines() == [
        "elapsed: end is 0 days 7 hours 0 minutes after start",
        "total: 7 hours",
    ]


@pytest.mark.parametrize(
    "args",
    [
        {"start": "2026-11-01", "op": "between", "end": "2026-11-02T00:00"},
        {"start": "2026-11-01", "op": "add", "add": {"hours": 1}},
        {"start": "9999-12-01", "op": "add", "add": {"months": 1}},
        {"start": "0001-01-01", "op": "between", "end": "9999-01-01"},
        {"start": "2026-11-01", "op": "between"},
        {"start": "not a date", "op": "add", "add": {"days": 1}},
    ],
)
async def test_what_cannot_be_answered_is_an_invalid_arguments_error(args: dict[str, Any]) -> None:
    assert read_tool_error_code(await _call("date_math", args)) is ToolErrorCode.INVALID_ARGUMENTS


@pytest.mark.parametrize(
    ("start", "end", "calendar"),
    [
        # Counted from start, so `between` undoes `add`: Mar 31 - 1 month is Feb 28.
        ("2026-03-31", "2026-02-28", "end is 1 month before start"),
        # Feb 15 is a month back, and past Feb 20: what is left is days.
        ("2026-03-15", "2026-02-20", "end is 23 days before start"),
        ("2026-01-31", "2026-02-28", "end is 1 month after start"),
        ("2026-01-31", "2026-03-01", "end is 1 month and 1 day after start"),
        ("2026-11-14", "2026-11-14", "end is 0 days after start"),
    ],
)
def test_the_calendar_difference_is_what_add_would_take(start: str, end: str, calendar: str) -> None:
    lines = T.date_math(T.DateMathArgs(start=start, op="between", end=end), default_zone="UTC").splitlines()
    assert lines[1] == f"calendar: {calendar}"


def test_a_shift_landing_in_a_gap_says_so() -> None:
    args = T.DateMathArgs(start="2026-03-01T02:30", op="add", add=T.Shift(weeks=1), zone="America/New_York")
    assert T.date_math(args, default_zone="UTC").splitlines() == [
        "result: 2026-03-08 03:30 Sunday · America/New_York · UTC-04:00 (EDT)",
        "note: 2026-03-08 02:30 does not exist in this zone (clocks go forward); read as 03:30",
    ]


def test_a_shift_landing_in_a_repeated_hour_says_so() -> None:
    args = T.DateMathArgs(start="2026-10-25T01:30", op="add", add=T.Shift(weeks=1), zone="America/New_York")
    assert T.date_math(args, default_zone="UTC").splitlines() == [
        "result: 2026-11-01 01:30 Sunday · America/New_York · UTC-04:00 (EDT)",
        "note: 2026-11-01 01:30 happens twice in this zone (clocks go back); "
        "the first was used, the second is UTC-05:00",
    ]


def test_a_start_in_a_gap_moves_its_wall_time_before_reading_it() -> None:
    # 02:30 on the 8th never happens; 02:30 on the 9th does, and is what a day later means.
    args = T.DateMathArgs(start="2026-03-08T02:30", op="add", add=T.Shift(days=1), zone="America/New_York")
    assert T.date_math(args, default_zone="UTC") == (
        "result: 2026-03-09 02:30 Monday · America/New_York · UTC-04:00 (EDT)"
    )


def test_an_hour_from_the_first_of_a_repeated_hour_is_the_second() -> None:
    args = T.DateMathArgs(start="2026-11-01T01:30", op="add", add=T.Shift(hours=1), zone="America/New_York")
    assert T.date_math(args, default_zone="UTC").splitlines() == [
        "result: 2026-11-01 01:30 Sunday · America/New_York · UTC-05:00 (EST)",
        "note: 2026-11-01 01:30 happens twice in this zone (clocks go back); "
        "the first was used, the second is UTC-05:00",
    ]


async def test_date_math_reads_a_bare_date_time_in_the_install_zone() -> None:
    out = output_text(
        await _call("date_math", {"start": "2026-11-01T00:00", "op": "add", "add": {"hours": 1}})
    )
    assert out == "result: 2026-11-01 01:00 Sunday · America/Phoenix · UTC-07:00 (MST)"


def test_an_offset_is_moved_into_an_explicit_zone() -> None:
    # 01:30-04:00 is 01:30 in New York before the change; a week on is 01:30 there, now EST.
    args = T.DateMathArgs(
        start="2026-11-01T01:30-04:00", op="add", add=T.Shift(days=7), zone="America/New_York"
    )
    assert T.date_math(args, default_zone="UTC") == (
        "result: 2026-11-08 01:30 Sunday · America/New_York · UTC-05:00 (EST)"
    )


def test_an_offset_without_a_zone_keeps_its_offset() -> None:
    args = T.DateMathArgs(start="2026-10-29T09:00-04:00", op="add", add=T.Shift(weeks=1))
    assert (
        T.date_math(args, default_zone="America/New_York") == "result: 2026-11-05 09:00 Thursday · UTC-04:00"
    )


def test_a_backwards_elapsed_time_says_which_way() -> None:
    args = T.DateMathArgs(
        start="2026-11-02T10:30", op="between", end="2026-10-30T09:00", zone="America/New_York"
    )
    assert T.date_math(args, default_zone="UTC").splitlines() == [
        "elapsed: end is 3 days 2 hours 30 minutes before start",
        "total: -74.5 hours",
    ]


def test_between_names_a_repeated_hour_it_read() -> None:
    args = T.DateMathArgs(
        start="2026-11-01T01:30", op="between", end="2026-11-01T03:00", zone="America/New_York"
    )
    assert T.date_math(args, default_zone="UTC").splitlines() == [
        "elapsed: end is 0 days 2 hours 30 minutes after start",
        "total: 2.5 hours",
        "note: 2026-11-01 01:30 happens twice in this zone (clocks go back); "
        "the first was used, the second is UTC-05:00",
    ]


def test_basic_format_dates_are_dates() -> None:
    args = T.DateMathArgs(start="20261114", op="between", end="2026-11-20")
    assert (
        T.date_math(args, default_zone="UTC").splitlines()[0]
        == "days: 6 (end is 0 weeks and 6 days after start)"
    )


def test_convert_says_from_zone_was_not_used_for_an_offset() -> None:
    args = T.TimeConvertArgs(time="2026-11-14T09:00+02:00", from_zone="Asia/Tokyo", to_zones=["UTC"])
    assert T.time_convert(args, default_zone="UTC").splitlines()[:2] == [
        "from: 2026-11-14 09:00 Saturday · UTC+02:00",
        "note: from_zone was not used: the time carries its own offset",
    ]


def test_part_hour_offsets_and_numeric_abbreviations() -> None:
    moment = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
    out = T.time_now(
        T.TimeNowArgs(zones=["Asia/Kathmandu", "America/Sao_Paulo"]), default_zone="UTC", now=moment
    )
    assert out.splitlines() == [
        "2026-10-30 17:45 Friday · Asia/Kathmandu · UTC+05:45",
        "2026-10-30 09:00 Friday · America/Sao_Paulo · UTC-03:00",
    ]


@pytest.mark.parametrize(
    "args",
    [
        {"start": "9999-12-31T23:00", "op": "add", "add": {"hours": 2}},
        {"start": "0001-01-01T00:00", "op": "between", "end": "0001-01-02T00:00", "zone": "Asia/Tokyo"},
    ],
)
async def test_a_date_time_past_the_calendar_is_refused_not_raised(args: dict[str, Any]) -> None:
    out = await _call("date_math", args)
    assert read_tool_error_code(out) is ToolErrorCode.INVALID_ARGUMENTS
    assert "outside years 1 to 9999" in output_text(out)
