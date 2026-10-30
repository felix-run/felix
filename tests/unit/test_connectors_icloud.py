"""`felix_connectors` iCloud tools: Calendar and Contacts, as the person a run acts for.

Each tool runs inside a request context acting for a person whose Apple ID and app-specific
password are in the vault, with iCloud's CalDAV and CardDAV played by two loopback servers
(`tests/support/icloud_fake`) through the real egress guard. What is checked is what iCloud would
get -- method, path, depth, body, the person's Basic credential -- and the answer as the model
reads it.
"""

from __future__ import annotations

import base64
import os
import time
import xml.etree.ElementTree as ET
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from tests.support.factories import make_settings
from tests.support.icloud_fake import (
    APP_PASSWORD,
    APPLE_ID,
    CARDS,
    EVENTS,
    MORE_CARDS,
    OLD,
    SHARED,
    WORK,
    FakeICloud,
    ics,
    vcard,
    vevent,
)
from tests.support.loopback_http import serve
from tests.support.optional_deps import require_optional

ME = "felix-self|google:1"
PHOENIX = timezone(timedelta(hours=-7))
START = datetime(2026, 11, 2, 8, 0, tzinfo=PHOENIX)
CR = "{urn:ietf:params:xml:ns:carddav}"


@pytest.fixture(autouse=True)
def _plugin() -> Any:
    require_optional("icalendar", "icloud-connectors")
    return require_optional("felix_connectors", "connectors")


def _settings() -> Any:
    return make_settings(connection_token_key=base64.b64encode(os.urandom(32)).decode())


async def _connect(settings: Any, apple_id: str = APPLE_ID, password: str = APP_PASSWORD) -> None:
    from felix.auth import connections

    await connections.save_secret(
        settings, "acme", person=ME, provider="icloud", account=apple_id, secret=password, label=apple_id
    )


def _as(settings: Any, person: str = ME) -> Any:
    from felix.context import AuthContext, RequestContext, async_run_with_context

    auth = AuthContext(
        principal_sub=person.partition("|")[2], tenant_id="acme", anonymous=False, person=person
    )
    return async_run_with_context(RequestContext(settings=settings, auth=auth))


@asynccontextmanager
async def _icloud(fake: FakeICloud) -> Any:
    """A `Dav` at the fake's two services, each on its own port, both on the loopback host."""
    from felix_connectors.icloud_dav import Dav

    async with serve(fake.caldav) as caldav, serve(fake.carddav) as carddav:
        yield Dav(
            caldav=caldav,
            carddav=carddav,
            hosts=frozenset({"127.0.0.1"}),
            user_agent="felix-test",
            allow_http=True,
        )


async def _run(
    fake: FakeICloud,
    tool: str,
    args: dict[str, Any],
    *,
    settings: Any = None,
    connect: bool = True,
    person: str = ME,
) -> Any:
    from felix_connectors.icloud import icloud_tools

    settings = settings or _settings()
    if connect:
        await _connect(settings)
    async with _icloud(fake) as dav, _as(settings, person):
        return await {t.name: t for t in icloud_tools(dav)}[tool].executor.execute(args)


def _error(out: Any) -> tuple[str | None, str]:
    from felix.tools.errors import read_tool_error_code
    from felix.tools.types import tool_output_content

    return read_tool_error_code(out), tool_output_content(out)


def _events(start: datetime = START, **args: Any) -> dict[str, Any]:
    return {"start": start.isoformat(), **args}


# --- Calendar: reading ---------------------------------------------------------------------

DENTIST = vevent("d1", "Dentist", "20261102T170000Z", "20261102T180000Z", "LOCATION:12 Main St")
LUNCH = vevent("l1", "Lunch", "20261103T190000Z", "20261103T200000Z")
STANDUP = vevent("s1", "Standup", "20261102T160000Z", "20261102T161500Z")
LATER = vevent("x1", "Next month", "20261210T170000Z", "20261210T180000Z")


async def test_events_from_every_calendar_are_merged_in_order_as_the_person() -> None:
    fake = FakeICloud()
    fake.events[EVENTS] = [ics(LUNCH), ics(DENTIST), ics(LATER)]
    fake.events[WORK] = [ics(STANDUP)]
    fake.events[SHARED] = [ics(vevent("f1", "Soccer", "20261102T230000Z", "20261103T000000Z"))]
    out = await _run(fake, "connectors__icloud_calendar_events", _events())
    assert out.splitlines() == [
        "source: iCloud Calendar",
        "- 2026-11-02T09:00:00-07:00 to 2026-11-02T09:15:00-07:00 · Standup · calendar Work · uid s1",
        "- 2026-11-02T10:00:00-07:00 to 2026-11-02T11:00:00-07:00 · Dentist · at 12 Main St · calendar Home · uid d1",
        "- 2026-11-02T16:00:00-07:00 to 2026-11-02T17:00:00-07:00 · Soccer · calendar Family · uid f1",
        "- 2026-11-03T12:00:00-07:00 to 2026-11-03T13:00:00-07:00 · Lunch · calendar Home · uid l1",
    ]
    assert fake.served["caldav"] == [
        "PROPFIND /",
        "PROPFIND /123/principal/",
        "PROPFIND /123/calendars/",
        f"REPORT {SHARED}",
        f"REPORT {EVENTS}",
        f"REPORT {WORK}",
        f"REPORT {OLD}",
    ]
    assert fake.served["carddav"] == []
    assert [r.headers.get("depth") for r in fake.seen] == ["0", "0", "1", "1", "1", "1", "1"]
    assert {r.headers["authorization"] for r in fake.seen} == {fake.auth}
    assert {r.headers["user-agent"] for r in fake.seen} == {"felix-test"}


async def test_the_limit_holds_across_calendars() -> None:
    fake = FakeICloud()
    fake.events[EVENTS] = [ics(DENTIST), ics(LUNCH)]
    fake.events[WORK] = [ics(STANDUP)]
    out = await _run(fake, "connectors__icloud_calendar_events", _events(limit=2))
    assert [line.split(" · ")[1] for line in out.splitlines()[1:]] == ["Standup", "Dentist"]


async def test_a_calendar_that_does_not_say_what_it_holds_is_read() -> None:
    fake = FakeICloud()
    fake.events[OLD] = [ics(DENTIST)]
    out = await _run(fake, "connectors__icloud_calendar_events", _events())
    assert out.splitlines()[1].endswith("· calendar Old · uid d1")


async def test_the_query_asks_the_server_for_the_window_in_utc() -> None:
    fake = FakeICloud()
    await _run(fake, "connectors__icloud_calendar_events", _events(days=2))
    report = fake.requests("REPORT")[0]
    assert report.headers["content-type"].startswith("application/xml")
    window = ET.fromstring(report.body).find(".//{urn:ietf:params:xml:ns:caldav}time-range")
    assert window is not None
    assert (window.get("start"), window.get("end")) == ("20261102T150000Z", "20261104T150000Z")


async def test_a_weekly_event_shows_on_each_day_it_falls() -> None:
    fake = FakeICloud()
    fake.events[EVENTS] = [
        ics(vevent("w1", "Gym", "20261026T160000Z", "20261026T163000Z", "RRULE:FREQ=WEEKLY"))
    ]
    out = await _run(fake, "connectors__icloud_calendar_events", _events(days=14))
    assert [line.split(" · ")[0] for line in out.splitlines()[1:]] == [
        "- 2026-11-02T09:00:00-07:00 to 2026-11-02T09:30:00-07:00",
        "- 2026-11-09T09:00:00-07:00 to 2026-11-09T09:30:00-07:00",
    ]


async def test_a_series_in_a_zone_keeps_its_wall_clock_across_daylight_time() -> None:
    # 09:00 in Los Angeles: PDT (16:00Z) on Oct 26, PST (17:00Z) from Nov 2 -- 10:00 in Phoenix.
    fake = FakeICloud()
    weekly = vevent(
        "w2",
        "Piano",
        ";TZID=America/Los_Angeles:20261026T090000",
        ";TZID=America/Los_Angeles:20261026T100000",
        "RRULE:FREQ=WEEKLY",
    )
    fake.events[EVENTS] = [ics(weekly)]
    out = await _run(fake, "connectors__icloud_calendar_events", _events(days=7))
    assert out.splitlines()[1].split(" · ")[0] == "- 2026-11-02T10:00:00-07:00 to 2026-11-02T11:00:00-07:00"


async def test_an_excluded_day_of_a_series_is_left_out() -> None:
    fake = FakeICloud()
    fake.events[EVENTS] = [
        ics(
            vevent(
                "w3",
                "Class",
                "20261102T170000Z",
                "20261102T180000Z",
                "RRULE:FREQ=WEEKLY",
                "EXDATE:20261109T170000Z",
            )
        )
    ]
    out = await _run(fake, "connectors__icloud_calendar_events", _events(days=21))
    assert [line[2:12] for line in out.splitlines()[1:]] == ["2026-11-02", "2026-11-16"]


async def test_a_floating_time_reads_as_the_askers_and_sorts_among_the_rest() -> None:
    fake = FakeICloud()
    fake.events[EVENTS] = [ics(DENTIST), ics(vevent("f2", "Coffee", "20261102T093000", "20261102T094500"))]
    out = await _run(fake, "connectors__icloud_calendar_events", _events())
    assert [line.split(" · ")[:2] for line in out.splitlines()[1:]] == [
        ["- 2026-11-02T09:30:00 to 2026-11-02T09:45:00", "Coffee"],
        ["- 2026-11-02T10:00:00-07:00 to 2026-11-02T11:00:00-07:00", "Dentist"],
    ]


async def test_an_all_day_event_reads_by_its_dates_and_sorts_by_the_askers_midnight() -> None:
    # The holiday starts at midnight in Phoenix (07:00Z), after an evening out the night before
    # (03:00Z): taken as UTC's midnight it would sort ahead of it.
    fake = FakeICloud()
    holiday = (
        "BEGIN:VEVENT\r\nUID:h1\r\nSUMMARY:Holiday\r\nDTSTART;VALUE=DATE:20261102\r\n"
        "DTEND;VALUE=DATE:20261103\r\nEND:VEVENT\r\n"
    )
    evening = vevent("e1", "Concert", "20261102T030000Z", "20261102T050000Z")  # Nov 1, 20:00 Phoenix
    fake.events[EVENTS] = [ics(holiday), ics(DENTIST), ics(evening)]
    out = await _run(fake, "connectors__icloud_calendar_events", _events(START - timedelta(hours=20)))
    assert [line.split(" · ")[:2] for line in out.splitlines()[1:]] == [
        ["- 2026-11-01T20:00:00-07:00 to 2026-11-01T22:00:00-07:00", "Concert"],
        ["- 2026-11-02 to 2026-11-03", "Holiday"],
        ["- 2026-11-02T10:00:00-07:00 to 2026-11-02T11:00:00-07:00", "Dentist"],
    ]


async def test_an_event_the_libraries_cannot_read_is_skipped_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO", logger="felix_connectors.icloud")
    fake = FakeICloud()
    fake.events[EVENTS] = ["not a calendar", ics(DENTIST)]
    out = await _run(fake, "connectors__icloud_calendar_events", _events())
    assert [line.split(" · ")[1] for line in out.splitlines()[1:]] == ["Dentist"]
    assert "icloud_event_skipped" in caplog.text
    assert "not a calendar" not in caplog.text


async def test_no_events_says_so() -> None:
    out = await _run(FakeICloud(), "connectors__icloud_calendar_events", _events())
    assert out.splitlines() == ["source: iCloud Calendar", "no events"]


@pytest.mark.parametrize(
    "rule",
    [
        "RRULE:FREQ=SECONDLY",
        "RRULE:FREQ=MINUTELY",
        "RRULE:FREQ=DAILY;BYHOUR=0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23;"
        "BYMINUTE=0,5,10,15,20,25,30,35,40,45,50,55",
    ],
)
async def test_a_series_too_costly_to_expand_is_left_out_quickly(rule: str) -> None:
    # A rule anyone can put on the person's calendar with an invitation, from long before the window.
    fake = FakeICloud()
    fake.events[EVENTS] = [
        ics(vevent("r1", "Spam", "20260101T000000Z", "20260101T000001Z", rule)),
        ics(DENTIST),
    ]
    began = time.monotonic()
    out = await _run(fake, "connectors__icloud_calendar_events", _events())
    assert time.monotonic() - began < 2
    assert out.splitlines()[1:] == [
        "- 2026-11-02T10:00:00-07:00 to 2026-11-02T11:00:00-07:00 · Dentist · at 12 Main St · calendar Home · uid d1",
        "(1 repeating events left out: they repeat too often to list)",
    ]


def test_years_of_an_ordinary_series_cost_little() -> None:
    import icalendar
    from felix_connectors.icloud import MAX_STEPS, expansion_steps

    def cost(rule: str) -> float:
        cal = icalendar.Calendar.from_ical(
            ics(vevent("a", "x", "20160101T090000Z", "20160101T100000Z", rule))
        )
        (event,) = cal.walk("VEVENT")
        return expansion_steps(event, datetime(2026, 12, 1, tzinfo=UTC))

    assert cost("RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR") < MAX_STEPS
    assert cost("RRULE:FREQ=DAILY") < MAX_STEPS
    assert cost("RRULE:FREQ=HOURLY") > MAX_STEPS
    assert cost("RRULE:FREQ=WEEKLY;INTERVAL=2") < cost("RRULE:FREQ=WEEKLY") < cost("RRULE:FREQ=DAILY")


# --- whose iCloud ----------------------------------------------------------------------------


async def test_a_person_with_no_icloud_connected_is_told_how() -> None:
    fake = FakeICloud()
    out = await _run(fake, "connectors__icloud_contacts_search", {"query": "ana"}, connect=False)
    assert (
        out.splitlines()[-1]
        == "the person has not connected an iCloud account; they can connect one in Felix"
    )
    assert fake.seen == []


async def test_with_two_apple_ids_the_named_one_is_used() -> None:
    fake = FakeICloud(apple_id="work@icloud.com", password="wxyz-wxyz-wxyz-wxyz")
    settings = _settings()
    await _connect(settings)
    await _connect(settings, "work@icloud.com", "wxyz-wxyz-wxyz-wxyz")
    unnamed = await _run(fake, "connectors__icloud_calendar_events", {}, settings=settings, connect=False)
    assert "name one with `account`" in unnamed.splitlines()[-1]
    assert fake.seen == []
    named = await _run(
        fake,
        "connectors__icloud_calendar_events",
        {"account": "work@icloud.com"},
        settings=settings,
        connect=False,
    )
    assert named.splitlines() == ["source: iCloud Calendar", "no events"]
    assert {r.headers["authorization"] for r in fake.seen} == {fake.auth}


async def test_a_password_apple_no_longer_accepts_says_reconnect() -> None:
    fake = FakeICloud(password="zzzz-zzzz-zzzz-zzzz")
    out = await _run(fake, "connectors__icloud_calendar_events", {})
    assert out.splitlines() == [
        "source: iCloud Calendar",
        "Apple no longer accepts this app-specific password; the person must connect iCloud again",
    ]
    assert len(fake.seen) == 1


async def test_a_disconnected_icloud_is_not_used() -> None:
    from felix.auth import connections

    fake = FakeICloud()
    settings = _settings()
    await _connect(settings)
    await connections.remove_connection(settings, "acme", ME, "icloud", APPLE_ID)
    out = await _run(fake, "connectors__icloud_calendar_events", {}, settings=settings, connect=False)
    assert (
        out.splitlines()[-1]
        == "the person has not connected an iCloud account; they can connect one in Felix"
    )
    assert fake.seen == []


# --- Calendar: adding -------------------------------------------------------------------------

EVENT_ARGS = {
    "summary": "Dinner with Ana",
    "start": "2026-11-05T18:00:00-07:00",
    "end": "2026-11-05T19:30:00-07:00",
    "description": "Bring the book",
    "location": "Pizzeria Bianco",
}


def _uid(args: dict[str, Any] = EVENT_ARGS, person: str = ME) -> str:
    from felix_connectors.icloud import CreateEventArgs, event_uid

    return event_uid(person, CreateEventArgs.model_validate(args))


async def test_an_event_goes_to_the_persons_own_first_calendar_with_no_guests() -> None:
    import icalendar

    fake = FakeICloud()
    out = await _run(fake, "connectors__icloud_calendar_create_event", EVENT_ARGS)
    ((path, body),) = fake.resources.items()
    assert path == f"{EVENTS}{_uid()}.ics"
    (put,) = fake.requests("PUT")
    assert (put.headers["content-type"], put.headers["if-none-match"]) == (
        "text/calendar; charset=utf-8",
        "*",
    )
    (event,) = icalendar.Calendar.from_ical(body).walk("VEVENT")
    assert str(event["SUMMARY"]) == "Dinner with Ana"
    assert event["DTSTART"].dt == datetime(2026, 11, 6, 1, 0, tzinfo=UTC)
    assert event["DTEND"].dt == datetime(2026, 11, 6, 2, 30, tzinfo=UTC)
    assert (str(event["DESCRIPTION"]), str(event["LOCATION"])) == ("Bring the book", "Pizzeria Bianco")
    assert "ATTENDEE" not in event and "ORGANIZER" not in event
    assert out.splitlines() == [
        "source: iCloud Calendar",
        "created: 2026-11-05T18:00:00-07:00 to 2026-11-05T19:30:00-07:00 · Dinner with Ana · calendar Home · "
        f"uid {_uid()}",
    ]


async def test_a_named_calendar_of_the_persons_own_takes_the_event() -> None:
    fake = FakeICloud()
    out = await _run(fake, "connectors__icloud_calendar_create_event", {**EVENT_ARGS, "calendar": "Work"})
    assert list(fake.resources) == [f"{WORK}{_uid()}.ics"]
    assert "· calendar Work ·" in out.splitlines()[-1]


@pytest.mark.parametrize(
    ("calendar", "said"),
    [
        ("Family", "the person has no iCloud calendar of their own named Family that takes events"),
        ("Reminders", "the person has no iCloud calendar of their own named Reminders that takes events"),
        ("Old", "the person has no iCloud calendar of their own named Old that takes events"),
        ("Nope", "the person has no iCloud calendar of their own named Nope that takes events"),
    ],
)
async def test_a_calendar_shared_read_only_or_absent_takes_nothing(calendar: str, said: str) -> None:
    # Family is shared with the person, Reminders holds to-dos, Old is read-only.
    fake = FakeICloud()
    out = await _run(fake, "connectors__icloud_calendar_create_event", {**EVENT_ARGS, "calendar": calendar})
    assert out.splitlines()[-1] == said
    assert fake.requests("PUT") == []


async def test_a_calendar_shared_with_the_person_takes_nothing_even_where_they_may_write() -> None:
    # Its owner reads it: an instruction planted in one of its events could have the agent copy
    # what else it read there. Listed first, it is still never the default.
    fake = FakeICloud()
    fake.shared_writable = True
    default = await _run(fake, "connectors__icloud_calendar_create_event", EVENT_ARGS)
    named = await _run(fake, "connectors__icloud_calendar_create_event", {**EVENT_ARGS, "calendar": "Family"})
    assert list(fake.resources) == [f"{EVENTS}{_uid()}.ics"]
    assert "· calendar Home ·" in default.splitlines()[-1]
    assert (
        named.splitlines()[-1]
        == "the person has no iCloud calendar of their own named Family that takes events"
    )


async def test_two_calendars_of_one_name_are_not_guessed_between() -> None:
    fake = FakeICloud()
    fake.names[WORK] = "Home"
    out = await _run(fake, "connectors__icloud_calendar_create_event", {**EVENT_ARGS, "calendar": "Home"})
    assert out.splitlines()[-1] == "the person has several calendars named Home; they can rename one"
    assert fake.requests("PUT") == []


@pytest.mark.parametrize("extra", [{"attendees": ["attacker@example.com"]}, {"organizer": "me@icloud.com"}])
async def test_an_event_cannot_carry_guests(extra: dict[str, Any]) -> None:
    fake = FakeICloud()
    out = await _run(fake, "connectors__icloud_calendar_create_event", {**EVENT_ARGS, **extra})
    assert _error(out)[0] == "invalid_arguments"
    assert fake.seen == []


async def test_the_same_event_twice_is_added_once() -> None:
    fake = FakeICloud()
    settings = _settings()
    first = await _run(fake, "connectors__icloud_calendar_create_event", EVENT_ARGS, settings=settings)
    again = await _run(
        fake, "connectors__icloud_calendar_create_event", EVENT_ARGS, settings=settings, connect=False
    )
    assert list(fake.resources) == [f"{EVENTS}{_uid()}.ics"]
    assert first.splitlines()[-1].endswith(f"uid {_uid()}")
    assert again.splitlines()[-1] == f"this event is already in the calendar (uid {_uid()})"


def test_an_events_uid_is_its_persons_its_title_and_its_times() -> None:
    variants = [
        _uid(),
        _uid(person="felix-self|google:2"),
        _uid({**EVENT_ARGS, "summary": "Lunch with Ana"}),
        _uid({**EVENT_ARGS, "start": "2026-11-05T17:00:00-07:00"}),
        _uid({**EVENT_ARGS, "end": "2026-11-05T20:00:00-07:00"}),
    ]
    assert _uid() == _uid(dict(EVENT_ARGS))
    assert len(set(variants)) == 5


async def test_the_uid_is_the_person_the_run_acts_for() -> None:
    fake = FakeICloud()
    await _run(fake, "connectors__icloud_calendar_create_event", EVENT_ARGS, person=ME)
    assert list(fake.resources) == [f"{EVENTS}{_uid(person=ME)}.ics"]


async def test_a_calendar_that_refuses_the_event_is_said_not_blamed_on_the_password() -> None:
    fake = FakeICloud()
    fake.raw[("PUT", f"{EVENTS}{_uid()}.ics")] = ("403 Forbidden", b"")
    out = await _run(fake, "connectors__icloud_calendar_create_event", EVENT_ARGS)
    assert out.splitlines()[-1] == "iCloud did not let this calendar take the event"


async def test_a_redirected_put_is_no_event_created() -> None:
    fake = FakeICloud()
    fake.raw[("PUT", f"{EVENTS}{_uid()}.ics")] = ("302 Found", b"")
    out = await _run(fake, "connectors__icloud_calendar_create_event", EVENT_ARGS)
    assert _error(out) == (
        "transport_unavailable",
        "[tool error/transport_unavailable] iCloud Calendar answered 302",
    )


def test_adding_is_neither_read_only_nor_replay_safe_and_reading_is_both() -> None:
    from felix_connectors.icloud import icloud_tools
    from felix_connectors.icloud_dav import Dav

    tools = {t.name: t for t in icloud_tools(Dav())}
    adding = tools["connectors__icloud_calendar_create_event"]
    assert (adding.read_only, adding.replay_safe) == (False, False)
    reading = [tools["connectors__icloud_calendar_events"], tools["connectors__icloud_contacts_search"]]
    assert {(t.read_only, t.replay_safe) for t in reading} == {(True, True)}


# --- Contacts ---------------------------------------------------------------------------------


async def test_contacts_are_searched_in_each_address_book_on_carddav() -> None:
    fake = FakeICloud()
    fake.cards[CARDS] = [
        vcard(
            "Ana Lima", "EMAIL;TYPE=HOME:ana@example.com", "TEL;TYPE=CELL:+1 602 555 0100", "ORG:Lima & Co"
        ),
    ]
    fake.cards[MORE_CARDS] = [vcard("Bo Chen")]
    out = await _run(fake, "connectors__icloud_contacts_search", {"query": "a"})
    assert out.splitlines() == [
        "source: iCloud Contacts",
        "- Ana Lima · email ana@example.com · tel +1 602 555 0100 · Lima & Co",
        "- Bo Chen",
    ]
    assert fake.served["carddav"] == [
        "PROPFIND /",
        "PROPFIND /123/principal/",
        "PROPFIND /123/carddavhome/",
        f"REPORT {CARDS}",
        f"REPORT {MORE_CARDS}",
    ]
    assert fake.served["caldav"] == []
    assert [r.headers.get("depth") for r in fake.requests("REPORT")] == ["1", "1"]


async def test_the_contacts_query_carries_the_words_as_text_any_field_containing_them() -> None:
    fake = FakeICloud()
    words = 'x</cr:text-match><cr:prop-filter name="NOTE"/>&'
    await _run(fake, "connectors__icloud_contacts_search", {"query": words, "limit": 3})
    root = ET.fromstring(fake.requests("REPORT")[0].body)
    assert root.find(f"{CR}filter").get("test") == "anyof"
    filters = root.findall(f".//{CR}prop-filter")
    assert [f.get("name") for f in filters] == ["FN", "EMAIL", "TEL"]
    matches = [f.find(f"{CR}text-match") for f in filters]
    assert {(m.text, m.get("match-type"), m.get("collation")) for m in matches} == {
        (words, "contains", "i;unicode-casemap")
    }
    assert root.findtext(f".//{CR}nresults") == "3"


async def test_no_more_contacts_than_the_limit_across_books_and_no_book_asked_past_it() -> None:
    fake = FakeICloud()
    fake.cards[CARDS] = ["BEGIN:VCARD\r\nthis is no card", vcard("A")]
    fake.cards[MORE_CARDS] = [vcard("B"), vcard("C")]
    two = await _run(fake, "connectors__icloud_contacts_search", {"query": "x", "limit": 2})
    assert two.splitlines()[1:] == ["- A", "- B"]
    fake.served["carddav"].clear()
    one = await _run(fake, "connectors__icloud_contacts_search", {"query": "x", "limit": 1})
    assert one.splitlines()[1:] == ["- A"]
    assert f"REPORT {MORE_CARDS}" not in fake.served["carddav"]


async def test_no_contacts_says_so() -> None:
    out = await _run(FakeICloud(), "connectors__icloud_contacts_search", {"query": "zed"})
    assert out.splitlines() == ["source: iCloud Contacts", "no contacts"]


# --- what comes back, and where requests go --------------------------------------------------


async def test_an_answer_carrying_a_dtd_is_refused_unparsed() -> None:
    fake = FakeICloud()
    laughs = (
        b'<?xml version="1.0"?><!DOCTYPE d [<!ENTITY a "/123/principal/">]><d:multistatus xmlns:d="DAV:">'
        b"<d:response><d:href>/</d:href><d:propstat><d:prop><d:current-user-principal><d:href>&a;</d:href>"
        b"</d:current-user-principal></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
        b"</d:multistatus>"
    )
    fake.raw[("PROPFIND", "/")] = ("207 Multi-Status", laughs)
    out = await _run(fake, "connectors__icloud_calendar_events", {})
    assert _error(out) == (
        "provider_error",
        "[tool error/provider_error] iCloud Calendar gave an unusable answer",
    )
    assert len(fake.seen) == 1


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
def test_a_dtd_is_found_whatever_the_bodys_encoding(encoding: str) -> None:
    from felix_connectors.icloud_dav import multistatus

    body = f'<?xml version="1.0" encoding="{encoding}"?><!DOCTYPE d><d:multistatus xmlns:d="DAV:"/>'
    with pytest.raises(ValueError, match="DTD"):
        multistatus(body.encode(encoding), "https://caldav.icloud.com/")


async def test_a_home_iCloud_names_on_an_undeclared_host_is_refused() -> None:
    fake = FakeICloud()
    async with serve(fake.caldav) as elsewhere:
        fake.home_host = elsewhere.replace("127.0.0.1", "localhost")
        out = await _run(fake, "connectors__icloud_calendar_events", {})
    assert _error(out)[0] == "permission_denied"
    assert fake.served["caldav"] == ["PROPFIND /", "PROPFIND /123/principal/"]


async def test_without_the_development_exemption_plain_http_is_never_sent() -> None:
    from felix.security.ssrf import EgressBlocked
    from felix_connectors.icloud_dav import Dav

    fake = FakeICloud()
    async with serve(fake.caldav) as base:
        dav = Dav(caldav=base, hosts=frozenset({"127.0.0.1"}), allow_http=False)
        with pytest.raises(EgressBlocked):
            await dav.principal(dav.caldav, fake.auth)
    assert fake.seen == []


@pytest.mark.parametrize(("insecure", "plain"), [(False, False), (True, True)])
def test_the_production_endpoints_are_apples_and_every_partition_is_declared(
    insecure: bool, plain: bool
) -> None:
    from felix_connectors.http import user_agent
    from felix_connectors.icloud import icloud_tools
    from felix_connectors.icloud_dav import Dav

    settings = make_settings(allow_insecure=insecure, connectors_contact="ops@example.com")
    dav = Dav.from_settings(settings)
    assert (dav.caldav, dav.carddav) == ("https://caldav.icloud.com", "https://contacts.icloud.com")
    assert (dav.user_agent, dav.allow_http) == (user_agent(settings), plain)
    assert {t.egress_hosts for t in icloud_tools(dav)} == {frozenset({"*.icloud.com"})}


def test_the_declared_hosts_cover_apples_partition_hosts_and_nothing_beside() -> None:
    from felix.security.host_allowlist import HostAllowlist
    from felix_connectors.icloud_dav import ICLOUD_HOSTS

    (pattern,) = ICLOUD_HOSTS
    held = HostAllowlist((pattern,))
    hosts = ("caldav.icloud.com", "p42-caldav.icloud.com", "icloud.com.evil.example")
    assert [held.allows(h) for h in hosts] == [True, True, False]


# --- the credential ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "apple_id",
    ["", "me", "me:x@icloud.com", "me @icloud.com", "me\r\n@icloud.com", "a" * 310 + "@icloud.com"],
)
def test_an_apple_id_that_is_no_email_or_would_break_the_credential_is_refused(apple_id: str) -> None:
    from felix_connectors.icloud_dav import check_apple_id

    with pytest.raises(ValueError, match="Apple ID"):
        check_apple_id(apple_id)


def test_an_apple_id_is_kept_trimmed_and_lowercase() -> None:
    from felix_connectors.icloud_dav import check_apple_id

    assert check_apple_id("  Me@iCloud.com ") == "me@icloud.com"


@pytest.mark.parametrize(
    "password",
    [
        "hunter2",
        "ABCD-EFGH-IJKL-MNOP",
        "abcd-efgh-ijkl",
        "abcdefghijklmnop",
        "abcd-efgh-ijkl-mnopq",
        "xabcd-efgh-ijkl-mnop",
    ],
)
def test_anything_but_an_app_specific_password_is_refused(password: str) -> None:
    from felix_connectors.icloud_dav import check_app_password

    with pytest.raises(ValueError, match="app-specific password"):
        check_app_password(password)


def test_an_app_specific_password_is_kept_trimmed() -> None:
    from felix_connectors.icloud_dav import check_app_password

    assert check_app_password(f" {APP_PASSWORD} ") == APP_PASSWORD


def test_without_the_icloud_extra_a_tool_fails_by_the_extra_it_needs(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    from felix_connectors import icloud

    monkeypatch.setitem(sys.modules, "vobject", None)
    with pytest.raises(ValueError, match=r"felix-connectors\[icloud\] extra"):
        icloud.build_tools(make_settings())


def test_the_plugin_registers_every_icloud_tool_by_the_name_it_builds() -> None:
    from felix_connectors import FAMILIES, icloud

    assert icloud in FAMILIES
    assert set(icloud.TOOL_NAMES) == {t.name for t in icloud.build_tools(make_settings())}
    assert len(icloud.TOOL_NAMES) == 3


def test_without_the_icloud_extra_the_connect_route_is_not_mounted(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    from fastapi import FastAPI
    from felix_connectors import icloud_connect

    app = FastAPI()
    app.state.settings = make_settings()
    monkeypatch.setitem(sys.modules, "icalendar", None)
    icloud_connect.mount(app)
    assert icloud_connect.MOUNT not in app.openapi()["paths"]
    monkeypatch.delitem(sys.modules, "icalendar")
    app.openapi_schema = None
    icloud_connect.mount(app)
    assert icloud_connect.MOUNT in app.openapi()["paths"]
