"""`felix_connectors` calendar feeds: `connectors__ics_events`, as the person a run acts for.

Each call runs inside a request context acting for a person whose feed links are in the vault,
with the calendar services played by a loopback server through the real egress guard. The links
carry a secret path, as Outlook's and Google's do; what is checked is what the service was asked
and what the model reads -- never the link.
"""

from __future__ import annotations

import base64
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from tests.support.factories import make_settings
from tests.support.icloud_fake import ics, vevent
from tests.support.loopback_http import Request, respond, serve
from tests.support.optional_deps import require_optional

ME = "felix-self|google:1"
PHOENIX = timezone(timedelta(hours=-7))
START = datetime(2026, 11, 2, 8, 0, tzinfo=PHOENIX)
WORK = "/owa/calendar/abc/s3cr3t-work-token/calendar.ics"
FAMILY = "/calendar/ical/family%40example.com/private-s3cr3t-family/basic.ics"

STANDUP = vevent("s1", "Standup", "20261102T163000Z", "20261102T164500Z", "RRULE:FREQ=WEEKLY;BYDAY=MO,WE")
REVIEW = vevent("r1", "Design review", "20261103T200000Z", "20261103T210000Z", "LOCATION:Room 4")
DINNER = vevent("d1", "Family dinner", "20261102T020000Z", "20261102T030000Z")
PICKUP = vevent("p1", "School pickup", "20261103T220000Z", "20261103T223000Z")


@pytest.fixture(autouse=True)
def _plugin() -> Any:
    require_optional("icalendar", "ics-connectors")
    return require_optional("felix_connectors", "connectors")


@dataclass
class FakeFeeds:
    """Calendar services at one loopback address: a feed per secret path, or a status for it."""

    feeds: dict[str, str] = field(default_factory=dict)
    statuses: dict[str, str] = field(default_factory=dict)
    seen: list[Request] = field(default_factory=list)

    def __call__(self, req: Request, writer: Any) -> None:
        self.seen.append(req)
        if req.path in self.statuses:
            respond(writer, b"no", status=self.statuses[req.path])
        elif req.path in self.feeds:
            respond(writer, self.feeds[req.path].encode(), ctype="text/calendar")
        else:
            respond(writer, b"no", status="404 Not Found")


def _settings() -> Any:
    return make_settings(connection_token_key=base64.b64encode(os.urandom(32)).decode())


async def _connect(settings: Any, label: str, url: str) -> None:
    from felix.auth import connections
    from felix_connectors.ics import account_of

    await connections.save_secret(
        settings, "acme", person=ME, provider="ics", account=account_of(label), secret=url, label=label
    )


def _as(settings: Any, person: str = ME) -> Any:
    from felix.context import AuthContext, RequestContext, async_run_with_context

    auth = AuthContext(
        principal_sub=person.partition("|")[2], tenant_id="acme", anonymous=False, person=person
    )
    return async_run_with_context(RequestContext(settings=settings, auth=auth))


@asynccontextmanager
async def _feeds(fake: FakeFeeds, hosts: frozenset[str] = frozenset({"127.0.0.1"})) -> AsyncIterator[Any]:
    from felix_connectors.ics import Feeds

    async with serve(fake) as base:
        yield base, Feeds(hosts=hosts, user_agent="felix-test", allow_http=True)


async def _run(
    fake: FakeFeeds, args: dict[str, Any], *, links: dict[str, str], person: str = ME, **kw: Any
) -> str:
    """The tool's answer, after connecting each `label -> path` in `links` at the fake."""
    from felix.tools.types import tool_output_content
    from felix_connectors.ics import ics_tools

    settings = _settings()
    async with _feeds(fake, **kw) as (base, feeds):
        for label, path in links.items():
            await _connect(settings, label, base + path)
        async with _as(settings, person):
            [tool] = ics_tools(feeds)
            return tool_output_content(await tool.executor.execute({"start": START.isoformat(), **args}))


async def test_events_from_every_feed_in_time_order_named_by_feed() -> None:
    fake = FakeFeeds(feeds={WORK: ics(STANDUP, REVIEW), FAMILY: ics(DINNER, PICKUP)})
    out = await _run(fake, {"days": 3}, links={"Work": WORK, "Family": FAMILY})
    assert out.splitlines() == [
        "source: Calendar feeds",
        "- 2026-11-02T09:30:00-07:00 to 2026-11-02T09:45:00-07:00 · Standup · calendar Work · uid s1",
        "- 2026-11-03T13:00:00-07:00 to 2026-11-03T14:00:00-07:00 · Design review · at Room 4 · calendar Work · uid r1",
        "- 2026-11-03T15:00:00-07:00 to 2026-11-03T15:30:00-07:00 · School pickup · calendar Family · uid p1",
        "- 2026-11-04T09:30:00-07:00 to 2026-11-04T09:45:00-07:00 · Standup · calendar Work · uid s1",
    ]
    # Each feed fetched by its link, once, asking for iCalendar.
    assert sorted(r.path for r in fake.seen) == sorted([WORK, FAMILY])
    assert {r.headers.get("accept") for r in fake.seen} == {"text/calendar"}


async def test_one_feed_by_its_name() -> None:
    fake = FakeFeeds(feeds={WORK: ics(STANDUP), FAMILY: ics(PICKUP)})
    out = await _run(fake, {"feed": "  family ", "days": 3}, links={"Work": WORK, "Family": FAMILY})
    assert "School pickup" in out and "Standup" not in out
    assert [r.path for r in fake.seen] == [FAMILY]


async def test_a_feed_that_fails_is_said_and_the_rest_still_read() -> None:
    fake = FakeFeeds(feeds={FAMILY: ics(PICKUP)}, statuses={WORK: "404 Not Found"})
    out = await _run(fake, {"days": 3}, links={"Work": WORK, "Family": FAMILY})
    assert "School pickup" in out
    assert out.splitlines()[-1] == (
        "(feed Work not read: its calendar service answered 404; the link may have been withdrawn, "
        "so the person may need to connect it again)"
    )
    assert "s3cr3t" not in out and "127.0.0.1" not in out


async def test_a_kept_link_on_a_host_no_longer_allowed_is_not_fetched() -> None:
    fake = FakeFeeds(feeds={WORK: ics(STANDUP)})
    out = await _run(fake, {}, links={"Work": WORK}, hosts=frozenset({"calendar.example.com"}))
    assert out.splitlines()[-1] == "(feed Work not read: this deployment no longer reads feeds from its host)"
    assert fake.seen == []


async def test_a_rule_too_costly_to_expand_is_left_out_and_said() -> None:
    every_second = vevent("x", "Spam", "19700101T000000Z", "19700101T000001Z", "RRULE:FREQ=SECONDLY")
    fake = FakeFeeds(feeds={WORK: ics(every_second, REVIEW)})
    out = await _run(fake, {"days": 3}, links={"Work": WORK})
    assert "Design review" in out and "Spam" not in out
    assert "(1 events left out: they repeat too often, or the calendar holds too many, to list)" in out


async def test_a_feed_past_the_cap_is_not_read(monkeypatch: pytest.MonkeyPatch) -> None:
    from felix_connectors import ics as feeds_module

    monkeypatch.setattr(feeds_module, "MAX_FEED_BYTES", 200)
    fake = FakeFeeds(feeds={WORK: ics(STANDUP, REVIEW)})
    out = await _run(fake, {}, links={"Work": WORK})
    assert out.splitlines()[-1] == "(feed Work not read: it is larger than this deployment reads)"


@pytest.mark.parametrize(
    ("args", "links", "person", "said"),
    [
        ({}, {}, ME, "the person has not connected a calendar feed; they can connect one in Felix"),
        ({"feed": "Gym"}, {"Work": WORK}, ME, "the person has not connected a calendar feed named Gym"),
        ({}, {"Work": WORK}, "", "this run acts for no one person, so there is no calendar feed to use"),
    ],
)
async def test_what_cannot_be_read_is_said_in_words(
    args: dict[str, Any], links: dict[str, str], person: str, said: str
) -> None:
    fake = FakeFeeds(feeds={WORK: ics(STANDUP)})
    out = await _run(fake, args, links=links, person=person)
    assert out == f"source: Calendar feed\n{said}"
    assert fake.seen == []


async def test_another_persons_feed_is_not_theirs() -> None:
    fake = FakeFeeds(feeds={WORK: ics(STANDUP)})
    out = await _run(fake, {}, links={"Work": WORK}, person="felix-self|google:2")
    assert "has not connected a calendar feed" in out
    assert fake.seen == []


def test_the_tool_declares_the_operators_hosts() -> None:
    from felix_connectors.ics import build_tools

    [tool] = build_tools(make_settings(ics_hosts="calendar.example.com, *.feeds.example.org"))
    assert tool.egress_hosts == frozenset({"calendar.example.com", "*.feeds.example.org"})
    assert tool.read_only and tool.executor.transport == "connectors"


def test_the_default_hosts_are_where_calendars_publish() -> None:
    assert (
        make_settings().ics_hosts == "outlook.office365.com,outlook.live.com,calendar.google.com,*.icloud.com"
    )


@pytest.mark.parametrize("bad", ["*.com", "https://calendar.example.com", "cal endar.example.com"])
def test_a_hosts_setting_that_is_not_patterns_is_refused_at_load(bad: str) -> None:
    with pytest.raises(ValueError, match="FELIX_ICS_HOSTS"):
        make_settings(ics_hosts=bad)


HOSTS = frozenset({"outlook.office365.com", "*.icloud.com"})


@pytest.mark.parametrize(
    ("url", "kept"),
    [
        ("webcal://p42-caldav.icloud.com/published/2/abc", "https://p42-caldav.icloud.com/published/2/abc"),
        ("https://outlook.office365.com/owa/calendar/x/y/calendar.ics", None),
        ("  https://outlook.office365.com/a.ics?k=v  ", "https://outlook.office365.com/a.ics?k=v"),
    ],
)
def test_a_link_on_an_allowed_host_is_kept_as_https(url: str, kept: str | None) -> None:
    from felix_connectors.ics import check_feed_url

    assert check_feed_url(url, HOSTS) == (kept or url.strip())


@pytest.mark.parametrize(
    "url",
    [
        "http://outlook.office365.com/a.ics",
        "https://user:pw@outlook.office365.com/a.ics",
        "https://outlook.office365.com/a.ics#frag",
        "https://outlook.office365.com/a b.ics",
        "ftp://outlook.office365.com/a.ics",
        "not a link",
    ],
)
def test_a_link_of_any_other_shape_is_refused(url: str) -> None:
    from felix_connectors.ics import HostNotAllowed, check_feed_url

    with pytest.raises(ValueError) as refused:
        check_feed_url(url, HOSTS)
    assert not isinstance(refused.value, HostNotAllowed)


def test_plain_http_only_under_the_development_exemption() -> None:
    from felix_connectors.ics import check_feed_url

    assert (
        check_feed_url("http://outlook.office365.com/a.ics", HOSTS, plain=True)
        == "http://outlook.office365.com/a.ics"
    )


@pytest.mark.parametrize(("label", "kept"), [("  Work\n Calendar ", "Work Calendar"), ("Family", "Family")])
def test_a_feed_name_is_one_trimmed_line(label: str, kept: str) -> None:
    from felix_connectors.ics import check_label

    assert check_label(label) == kept


@pytest.mark.parametrize("label", ["", "   ", "Work\x07", "x" * 65])
def test_a_feed_name_that_cannot_be_shown_is_refused(label: str) -> None:
    from felix_connectors.ics import check_label

    with pytest.raises(ValueError):
        check_label(label)


@pytest.mark.parametrize("body", ["<html>sign in</html>", "", "BEGIN:VEVENT\r\nUID:x\r\nEND:VEVENT\r\n"])
def test_only_a_calendar_is_a_calendar(body: str) -> None:
    from felix_connectors.ics import NotACalendar, check_calendar

    with pytest.raises(NotACalendar):
        check_calendar(body)
    check_calendar(ics(STANDUP))


async def test_only_feed_connections_are_read_as_feeds() -> None:
    """A person's other secrets -- an iCloud app password -- are never taken for a feed link."""
    from felix.auth import connections
    from felix.tools.types import tool_output_content
    from felix_connectors.ics import ics_tools

    fake = FakeFeeds(feeds={WORK: ics(REVIEW)})
    settings = _settings()
    await connections.save_secret(
        settings, "acme", person=ME, provider="icloud", account="me@icloud.com", secret="abcd-efgh-ijkl-mnop"
    )
    async with _feeds(fake) as (base, feeds):
        await _connect(settings, "Work", base + WORK)
        async with _as(settings):
            [tool] = ics_tools(feeds)
            out = tool_output_content(await tool.executor.execute({"start": START.isoformat(), "days": 3}))
    assert "Design review" in out and "not read" not in out
    assert [r.path for r in fake.seen] == [WORK]


async def test_limit_caps_the_merged_list_not_each_feed() -> None:
    fake = FakeFeeds(feeds={WORK: ics(STANDUP, REVIEW), FAMILY: ics(PICKUP)})
    out = await _run(fake, {"days": 3, "limit": 2}, links={"Work": WORK, "Family": FAMILY})
    assert [line.split(" · ")[1] for line in out.splitlines()[1:]] == ["Standup", "Design review"]


async def test_the_window_ends_days_after_start() -> None:
    fake = FakeFeeds(feeds={WORK: ics(STANDUP, REVIEW)})
    out = await _run(fake, {"days": 1}, links={"Work": WORK})
    assert "Standup" in out and "Design review" not in out


async def test_an_all_day_event_sorts_at_midnight_where_the_person_is() -> None:
    # Nov 3 all day, against 03:00Z on Nov 3 -- 20:00 on Nov 2 in Phoenix, so it comes first.
    all_day = vevent("a1", "Holiday", ";VALUE=DATE:20261103", ";VALUE=DATE:20261104")
    evening = vevent("e1", "Late call", "20261103T030000Z", "20261103T033000Z")
    fake = FakeFeeds(feeds={WORK: ics(all_day, evening)})
    out = await _run(fake, {"days": 3}, links={"Work": WORK})
    assert out.splitlines()[1:] == [
        "- 2026-11-02T20:00:00-07:00 to 2026-11-02T20:30:00-07:00 · Late call · calendar Work · uid e1",
        "- 2026-11-03 to 2026-11-04 · Holiday · calendar Work · uid a1",
    ]


async def test_an_event_with_no_start_is_skipped_not_fatal() -> None:
    fake = FakeFeeds(feeds={WORK: ics("BEGIN:VEVENT\r\nUID:x\r\nSUMMARY:broken\r\nEND:VEVENT\r\n", REVIEW)})
    out = await _run(fake, {"days": 3}, links={"Work": WORK})
    assert "Design review" in out and "broken" not in out and "not read" not in out and "left out" not in out


async def test_a_feed_name_matches_whatever_its_case() -> None:
    fake = FakeFeeds(feeds={WORK: ics(REVIEW)})
    out = await _run(fake, {"feed": "WORK", "days": 3}, links={"Work": WORK})
    assert "Design review" in out


@pytest.mark.parametrize(
    ("budget", "value", "events"),
    [
        # Occurrences landing in the window: three one-offs, room for two.
        ("MAX_IN_WINDOW", 2, [STANDUP.replace("RRULE:FREQ=WEEKLY;BYDAY=MO,WE\r\n", ""), REVIEW, PICKUP]),
        # Steps walked across the calendar: two daily series since 1990, each under the per-series
        # cap, together over the calendar's.
        (
            "MAX_TOTAL_STEPS",
            15_000,
            [
                vevent("d1", "Daily one", "19900101T160000Z", "19900101T161000Z", "RRULE:FREQ=DAILY"),
                vevent("d2", "Daily two", "19900101T170000Z", "19900101T171000Z", "RRULE:FREQ=DAILY"),
            ],
        ),
        ("MAX_COMPONENTS", 1, [REVIEW, PICKUP]),
    ],
)
async def test_a_calendar_past_its_budget_leaves_the_rest_out_and_says_so(
    monkeypatch: pytest.MonkeyPatch, budget: str, value: int, events: list[str]
) -> None:
    from felix_connectors import ical

    monkeypatch.setattr(ical, budget, value)
    fake = FakeFeeds(feeds={WORK: ics(*events)})
    out = await _run(fake, {"days": 3}, links={"Work": WORK})
    assert (
        out.splitlines()[-1]
        == "(1 events left out: they repeat too often, or the calendar holds too many, to list)"
    )


async def test_past_one_offs_do_not_spend_the_window_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    from felix_connectors import ical

    monkeypatch.setattr(ical, "MAX_IN_WINDOW", 1)
    history = [vevent(f"h{i}", f"Old {i}", f"2020010{i}T160000Z", f"2020010{i}T170000Z") for i in range(1, 6)]
    fake = FakeFeeds(feeds={WORK: ics(*history, REVIEW)})
    out = await _run(fake, {"days": 3}, links={"Work": WORK})
    assert "Design review" in out and "left out" not in out


async def test_a_calendar_read_without_a_start_begins_now_in_the_deployments_zone() -> None:
    from felix_connectors.ical import default_start

    settings = make_settings(timezone="Pacific/Kiritimati")
    async with _as(settings):
        moment = default_start()
    assert str(moment.tzinfo) == "Pacific/Kiritimati"
    assert abs((moment - datetime.now(UTC)).total_seconds()) < 60


@pytest.mark.parametrize(
    "url",
    [
        "https://icloud.com/a.ics",
        "https://icloud.com.evil.example/a.ics",
        "https://outlook.office365.com.evil.example/a.ics",
    ],
)
def test_a_link_on_another_host_is_refused_as_such(url: str) -> None:
    from felix_connectors.ics import HostNotAllowed, check_feed_url

    with pytest.raises(HostNotAllowed):
        check_feed_url(url, HOSTS)


@pytest.mark.parametrize(
    "url",
    [
        "https://outlook.office365.com:22/a.ics",
        "https://outlook.office365.com:8443/a.ics",
        "https://outlook.office365.com:abc/a.ics",
    ],
)
def test_a_link_on_another_port_is_refused(url: str) -> None:
    from felix_connectors.ics import HostNotAllowed, check_feed_url

    with pytest.raises(ValueError) as refused:
        check_feed_url(url, HOSTS)
    assert not isinstance(refused.value, HostNotAllowed)
    assert check_feed_url("https://outlook.office365.com:443/a.ics", HOSTS)


@pytest.mark.parametrize("label", ["Home/Work", "Home\\Work"])
def test_a_feed_name_with_a_slash_is_refused(label: str) -> None:
    from felix_connectors.ics import check_label

    with pytest.raises(ValueError, match="no slash"):
        check_label(label)


def test_a_feed_is_kept_under_its_name_case_folded() -> None:
    from felix_connectors.ics import account_of

    assert account_of("Work") == account_of("WORK") == "work"


async def test_a_kept_link_that_now_serves_no_calendar_says_so() -> None:
    fake = FakeFeeds(feeds={WORK: "<html>signed out</html>", FAMILY: ics(PICKUP)})
    out = await _run(fake, {"days": 3}, links={"Work": WORK, "Family": FAMILY})
    assert "School pickup" in out
    assert out.splitlines()[-1] == (
        "(feed Work not read: what its link serves is not a calendar this deployment can read)"
    )
