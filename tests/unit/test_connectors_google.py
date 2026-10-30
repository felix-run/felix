"""`felix_connectors` Google tools: Calendar, Gmail and Drive, as the person a run acts for.

Each tool runs inside a request context acting for a person whose Google connection is in the
vault, with Google's APIs played by a loopback server through the real egress guard. Requests are
built by Google's own client against that server (its `api_endpoint`), so what is checked is the
request Google would get -- path, query, body, the person's token in the header -- and the answer
as the model reads it. Nothing here builds a URL by hand.
"""

from __future__ import annotations

import base64
import json
import os
from datetime import UTC, datetime, timedelta
from email import message_from_bytes
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.support.factories import make_settings
from tests.support.loopback_http import Request, respond, serve
from tests.support.optional_deps import require_optional

ME = "felix-self|google:1"
TOKEN = "ya29.the-persons-token"
CAL = "https://www.googleapis.com/auth/calendar.readonly"
EVENTS = "https://www.googleapis.com/auth/calendar.events"
GMAIL_READ = "https://www.googleapis.com/auth/gmail.readonly"
COMPOSE = "https://www.googleapis.com/auth/gmail.compose"
DRIVE = "https://www.googleapis.com/auth/drive.readonly"
EVERYTHING = f"openid email {CAL} {EVENTS} {GMAIL_READ} {COMPOSE} {DRIVE}"


@pytest.fixture(autouse=True)
def _plugin() -> Any:
    require_optional("googleapiclient", "google-connectors")
    return require_optional("felix_connectors", "connectors")


class FakeGoogleApis:
    """Google's REST APIs at a loopback address: a reply per (method, path), every request kept."""

    def __init__(self) -> None:
        self.seen: list[Request] = []
        self.replies: dict[tuple[str, str], tuple[str, Any]] = {}

    def reply(self, method: str, path: str, body: Any, status: str = "200 OK", *, alt: str = "") -> None:
        """`alt` tells a media download (`alt=media`) from the metadata read at the same path."""
        self.replies[(method, path + (f"?alt={alt}" if alt else ""))] = (status, body)

    def responder(self, request: Request, writer: Any) -> None:
        self.seen.append(request)
        path = urlsplit(request.path).path
        alt = parse_qs(urlsplit(request.path).query).get("alt", [""])[0]
        found = self.replies.get((request.method, f"{path}?alt={alt}")) or self.replies.get(
            (request.method, path)
        )
        status, body = found or ("404 Not Found", {})
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        respond(writer, raw, status=status, ctype="application/json")

    def query(self, i: int = -1) -> dict[str, list[str]]:
        return parse_qs(urlsplit(self.seen[i].path).query)


def _settings() -> Any:
    return make_settings(connection_token_key=base64.b64encode(os.urandom(32)).decode())


async def _connect(
    settings: Any,
    *,
    scopes: str = EVERYTHING,
    account: str = "1",
    label: str = "me@example.com",
    token: str = TOKEN,
) -> None:
    from felix.auth import connections
    from felix.auth.connections import OAuthGrant

    grant = OAuthGrant(access_token=token, expires_in=3600, refresh_token="1//refresh", scopes=scopes)
    await connections.save_oauth(
        settings, "acme", person=ME, provider="google", account=account, grant=grant, label=label
    )


def _as(settings: Any, person: str = ME) -> Any:
    from felix.context import AuthContext, RequestContext, async_run_with_context

    auth = AuthContext(
        principal_sub=person.partition("|")[2], tenant_id="acme", anonymous=False, person=person
    )
    return async_run_with_context(RequestContext(settings=settings, auth=auth))


def _tools(base: str) -> dict[str, Any]:
    from felix_connectors.gmail import gmail_tools
    from felix_connectors.google_calendar import calendar_tools
    from felix_connectors.google_common import GoogleApis
    from felix_connectors.google_drive import drive_tools

    apis = GoogleApis(googleapis=base, gmail=base, ua="felix-test", insecure=True)
    return {t.name: t for t in [*calendar_tools(apis), *gmail_tools(apis), *drive_tools(apis)]}


async def _run(
    fake: FakeGoogleApis, tool: str, args: dict[str, Any], *, settings: Any = None, person: str = ME
) -> Any:
    settings = settings or _settings()
    if not await _connections(settings):
        await _connect(settings)
    async with serve(fake.responder) as base, _as(settings, person):
        return await _tools(base)[tool].executor.execute(args)


async def _connections(settings: Any) -> list[Any]:
    from felix.auth import connections

    return await connections.list_for_person(settings, "acme", ME)


def _error(out: Any) -> tuple[str | None, str]:
    from felix.tools.errors import read_tool_error_code
    from felix.tools.types import tool_output_content

    return read_tool_error_code(out), tool_output_content(out)


# --- Calendar -----------------------------------------------------------------------------

EVENT = {
    "id": "evt1",
    "summary": "Dentist",
    "location": "12 Main St",
    "start": {"dateTime": "2026-10-30T09:00:00-07:00"},
    "end": {"dateTime": "2026-10-30T10:00:00-07:00"},
    "attendees": [{"email": "a@example.com"}],
}


async def test_list_events_asks_the_persons_primary_calendar_as_them() -> None:
    fake = FakeGoogleApis()
    fake.reply("GET", "/calendar/v3/calendars/primary/events", {"items": [EVENT, "junk"]})
    out = await _run(
        fake,
        "connectors__calendar_list_events",
        {"start": "2026-10-30T00:00:00-07:00", "days": 2, "limit": 3},
    )
    q = fake.query()
    assert (q["timeMin"], q["timeMax"], q["maxResults"], q["singleEvents"], q["orderBy"]) == (
        ["2026-10-30T00:00:00-07:00"],
        ["2026-11-01T00:00:00-07:00"],
        ["3"],
        ["true"],
        ["startTime"],
    )
    assert fake.seen[0].headers["authorization"] == f"Bearer {TOKEN}"
    assert out.splitlines() == [
        "source: Google Calendar",
        "- 2026-10-30T09:00:00-07:00 to 2026-10-30T10:00:00-07:00 · Dentist · at 12 Main St · 1 attendees · id evt1",
    ]
    assert TOKEN not in out


async def test_free_time_is_the_gaps_between_what_google_says_is_busy() -> None:
    fake = FakeGoogleApis()
    busy = [
        {"start": "2026-10-30T16:00:00Z", "end": "2026-10-30T17:00:00Z"},
        {"start": "2026-10-30T16:30:00Z", "end": "2026-10-30T18:00:00Z"},
        {"start": "bad"},
    ]
    fake.reply("POST", "/calendar/v3/freeBusy", {"calendars": {"primary": {"busy": busy}}})
    out = await _run(
        fake,
        "connectors__calendar_find_free_time",
        {"start": "2026-10-30T08:00:00-07:00", "end": "2026-10-30T12:00:00-07:00", "duration_minutes": 45},
    )
    assert json.loads(fake.seen[0].body or b"{}") == {
        "timeMin": "2026-10-30T08:00:00-07:00",
        "timeMax": "2026-10-30T12:00:00-07:00",
        "items": [{"id": "primary"}],
    }
    assert out.splitlines()[2:] == [
        "- 2026-10-30T08:00:00-07:00 to 2026-10-30T09:00:00-07:00",
        "- 2026-10-30T11:00:00-07:00 to 2026-10-30T12:00:00-07:00",
    ]


def test_free_windows_merge_overlaps_and_respect_the_duration() -> None:
    from felix_connectors.google_calendar import free_windows

    t = datetime(2026, 1, 1, 9, tzinfo=UTC)
    h = timedelta(hours=1)
    busy = [(t + h, t + 2 * h), (t + 1.5 * h, t + 3 * h), (t + 3.25 * h, t + 4 * h), (t + 9 * h, t + 10 * h)]
    assert free_windows(busy, t, t + 5 * h, 30) == [(t, t + h), (t + 4 * h, t + 5 * h)]
    assert free_windows([], t, t + h, 61) == []
    # A block inside an earlier, longer one does not end the busy time early.
    assert free_windows([(t, t + 3 * h), (t + h, t + 2 * h)], t, t + 4 * h, 30) == [(t + 3 * h, t + 4 * h)]


EVENT_ARGS = {"summary": "Dentist", "start": "2026-10-30T09:00:00-07:00", "end": "2026-10-30T10:00:00-07:00"}


async def test_an_event_is_created_with_no_guests_and_no_notification() -> None:
    from felix_connectors.google_calendar import CreateEventArgs, event_id

    fake = FakeGoogleApis()
    fake.reply("POST", "/calendar/v3/calendars/primary/events", EVENT)
    out = await _run(fake, "connectors__calendar_create_event", EVENT_ARGS)
    assert fake.query()["sendUpdates"] == ["none"]
    assert fake.seen[0].headers["content-type"].startswith("application/json")
    sent = json.loads(fake.seen[0].body or b"{}")
    assert sent == {
        "id": event_id(ME, CreateEventArgs.model_validate(EVENT_ARGS)),
        "summary": "Dentist",
        "start": {"dateTime": "2026-10-30T09:00:00-07:00"},
        "end": {"dateTime": "2026-10-30T10:00:00-07:00"},
        "description": "",
        "location": "",
    }
    assert out.splitlines()[1] == (
        "created: 2026-10-30T09:00:00-07:00 to 2026-10-30T10:00:00-07:00 · Dentist · at 12 Main St · 1 attendees · id evt1"
    )


@pytest.mark.parametrize("extra", [{"attendees": ["attacker@example.com"]}, {"notify_attendees": True}])
async def test_an_event_cannot_carry_guests(extra: dict[str, Any]) -> None:
    # An invitation is mail from the person's account; an injected instruction could address one.
    fake = FakeGoogleApis()
    out = await _run(fake, "connectors__calendar_create_event", {**EVENT_ARGS, **extra})
    assert fake.seen == []
    assert _error(out)[0] == "invalid_arguments"


def test_an_events_id_is_what_it_is_so_a_retry_names_the_same_one() -> None:
    from felix_connectors.google_calendar import CreateEventArgs, event_id

    one = CreateEventArgs.model_validate(EVENT_ARGS)
    other = CreateEventArgs.model_validate({**EVENT_ARGS, "summary": "Doctor"})
    assert event_id(ME, one) == event_id(ME, one)
    assert len({event_id(ME, one), event_id(ME, other), event_id("felix-self|google:2", one)}) == 3
    # Calendar's grammar: base32hex, lowercase, 5 to 1024 characters.
    assert set(event_id(ME, one)) <= set("0123456789abcdefghijklmnopqrstuv")
    assert 5 <= len(event_id(ME, one)) <= 1024


async def test_a_repeated_create_finds_the_event_it_already_made() -> None:
    from felix_connectors.google_calendar import CreateEventArgs, event_id

    fake = FakeGoogleApis()
    fake.reply(
        "POST", "/calendar/v3/calendars/primary/events", {"error": {"code": 409}}, status="409 Conflict"
    )
    out = await _run(fake, "connectors__calendar_create_event", EVENT_ARGS)
    made = event_id(ME, CreateEventArgs.model_validate(EVENT_ARGS))
    assert out.splitlines()[-1] == f"this event is already in the calendar (id {made})"


@pytest.mark.parametrize(
    "args",
    [
        {
            "summary": "x\r\nBcc: a@evil.example",
            "start": "2026-10-30T09:00:00Z",
            "end": "2026-10-30T10:00:00Z",
        },
        {"summary": "x", "start": "2026-10-30T10:00:00Z", "end": "2026-10-30T09:00:00Z"},
        {"summary": "x", "start": "2026-10-30T09:00:00", "end": "2026-10-30T10:00:00"},  # no offset
    ],
)
async def test_an_event_out_of_shape_never_reaches_google(args: dict[str, Any]) -> None:
    fake = FakeGoogleApis()
    out = await _run(fake, "connectors__calendar_create_event", args)
    assert fake.seen == []
    assert _error(out)[0] == "invalid_arguments"


# --- Gmail --------------------------------------------------------------------------------


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


MESSAGE = {
    "id": "m1",
    "snippet": "Your order\nhas shipped",
    "payload": {
        "headers": [
            {"name": "From", "value": "shop@example.com"},
            {"name": "Subject", "value": "Shipped"},
            {"name": "Date", "value": "Thu, 29 Oct 2026 10:00:00 -0700"},
            {"name": "To", "value": "me@example.com"},
        ],
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/html", "body": {"data": _b64("<p>html</p>")}},
            {"mimeType": "text/plain", "body": {"data": _b64("Line one\nLine two\x07")}},
        ],
    },
}


async def test_gmail_search_lists_then_reads_each_matchs_headers() -> None:
    fake = FakeGoogleApis()
    fake.reply("GET", "/gmail/v1/users/me/messages", {"messages": [{"id": "m1"}, {"id": "../etc"}, "junk"]})
    fake.reply("GET", "/gmail/v1/users/me/messages/m1", MESSAGE)
    out = await _run(fake, "connectors__gmail_search", {"query": "from:shop newer_than:7d", "limit": 3})
    assert fake.query(0)["q"] == ["from:shop newer_than:7d"]
    assert fake.query(1)["format"] == ["metadata"]
    assert len(fake.seen) == 2, "the id that is not one was never asked for"
    assert out.splitlines() == [
        "source: Gmail",
        "- Thu, 29 Oct 2026 10:00:00 -0700 · from shop@example.com · Shipped · id m1",
        "  Your order has shipped",
    ]


async def test_reading_mail_gives_the_plain_text_body() -> None:
    fake = FakeGoogleApis()
    fake.reply("GET", "/gmail/v1/users/me/messages/m1", MESSAGE)
    out = await _run(fake, "connectors__gmail_read", {"message_id": "m1"})
    assert fake.query()["format"] == ["full"]
    assert out.splitlines() == [
        "source: Gmail",
        "from: shop@example.com",
        "to: me@example.com",
        "date: Thu, 29 Oct 2026 10:00:00 -0700",
        "subject: Shipped",
        "",
        "Line one",
        "Line two ",
    ]


async def test_a_long_body_is_cut() -> None:
    from felix_connectors.person import MAX_TEXT

    fake = FakeGoogleApis()
    long = {**MESSAGE, "payload": {"mimeType": "text/plain", "body": {"data": _b64("x" * (MAX_TEXT + 50))}}}
    fake.reply("GET", "/gmail/v1/users/me/messages/m1", long)
    out = await _run(fake, "connectors__gmail_read", {"message_id": "m1"})
    assert out.endswith("x" * 10 + "\n(… cut)")


async def test_a_draft_is_a_mime_message_and_is_never_sent() -> None:
    fake = FakeGoogleApis()
    fake.reply("POST", "/gmail/v1/users/me/drafts", {"id": "r1"})
    out = await _run(
        fake,
        "connectors__gmail_draft",
        {"to": ["ana@example.com"], "subject": "Lunch?", "body": "Friday at noon?"},
    )
    raw = json.loads(fake.seen[0].body or b"{}")["message"]["raw"]
    mail = message_from_bytes(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    assert (mail["To"], mail["Subject"], mail.get_payload().strip()) == (
        "ana@example.com",
        "Lunch?",
        "Friday at noon?",
    )
    assert [r.path for r in fake.seen if "send" in r.path] == []
    assert out.splitlines() == ["source: Gmail", "draft saved (not sent), id r1"]


@pytest.mark.parametrize(
    "args",
    [
        {"to": ["ana@example.com"], "subject": "Hi\nBcc: everyone@example.com", "body": "x"},
        {"to": ["ana@example.com\r\nBcc: x@example.com"], "subject": "Hi", "body": "x"},
    ],
)
async def test_a_header_cannot_carry_another(args: dict[str, Any]) -> None:
    fake = FakeGoogleApis()
    out = await _run(fake, "connectors__gmail_draft", args)
    assert fake.seen == []
    assert _error(out)[0] == "invalid_arguments"


async def test_a_message_id_that_is_not_one_never_reaches_google() -> None:
    fake = FakeGoogleApis()
    out = await _run(fake, "connectors__gmail_read", {"message_id": "m1/../../drafts"})
    assert fake.seen == []
    assert _error(out)[0] == "invalid_arguments"


# --- Drive --------------------------------------------------------------------------------


async def test_drive_search_keeps_the_words_inside_its_own_string() -> None:
    fake = FakeGoogleApis()
    fake.reply("GET", "/drive/v3/files", {"files": [{"id": "f1", "name": "Plan", "mimeType": "text/plain"}]})
    out = await _run(fake, "connectors__drive_search", {"query": "q3' or name contains 'secret \\ x"})
    assert fake.query()["q"] == [
        "fullText contains 'q3\\' or name contains \\'secret \\\\ x' and trashed = false"
    ]
    assert out.splitlines()[1].startswith("- Plan · text/plain")


async def test_a_google_doc_is_exported_as_text_and_a_binary_file_left_alone() -> None:
    fake = FakeGoogleApis()
    fake.reply(
        "GET",
        "/drive/v3/files/doc1",
        {"id": "doc1", "name": "Notes", "mimeType": "application/vnd.google-apps.document"},
    )
    fake.reply("GET", "/drive/v3/files/doc1/export", b"Agenda\n1. Budget")
    settings = _settings()
    out = await _run(fake, "connectors__drive_read", {"file_id": "doc1"}, settings=settings)
    assert fake.query()["mimeType"] == ["text/plain"]
    assert out.splitlines() == ["source: Google Drive", "Notes", "", "Agenda", "1. Budget"]
    fake = FakeGoogleApis()
    fake.reply("GET", "/drive/v3/files/img1", {"id": "img1", "name": "Photo", "mimeType": "image/png"})
    out = await _run(fake, "connectors__drive_read", {"file_id": "img1"}, settings=settings)
    assert len(fake.seen) == 1
    assert out.splitlines()[-1] == "Photo is image/png, which is not text"


@pytest.mark.parametrize("kind", ["text/plain", "application/json"])
async def test_a_text_file_is_read_as_is(kind: str) -> None:
    from felix_connectors.person import MAX_TEXT

    fake = FakeGoogleApis()
    fake.reply("GET", "/drive/v3/files/t1", {"id": "t1", "name": "todo.txt", "mimeType": kind})
    fake.reply("GET", "/drive/v3/files/t1", b"buy milk\n" + b"x" * MAX_TEXT, alt="media")
    out = await _run(fake, "connectors__drive_read", {"file_id": "t1"})
    assert fake.query()["alt"] == ["media"]
    assert out.splitlines()[:4] == ["source: Google Drive", "todo.txt", "", "buy milk"]
    assert out.endswith("\n(… cut)")


async def test_a_spreadsheet_is_exported_as_csv() -> None:
    fake = FakeGoogleApis()
    sheet = {"id": "s1", "name": "Budget", "mimeType": "application/vnd.google-apps.spreadsheet"}
    fake.reply("GET", "/drive/v3/files/s1", sheet)
    fake.reply("GET", "/drive/v3/files/s1/export", b"a,b\n1,2")
    out = await _run(fake, "connectors__drive_read", {"file_id": "s1"})
    assert fake.query()["mimeType"] == ["text/csv"]
    assert out.splitlines()[-2:] == ["a,b", "1,2"]


async def test_drive_search_asks_for_the_fields_it_shows_and_no_more_than_the_limit() -> None:
    fake = FakeGoogleApis()
    files = [
        {"id": f"f{i}", "name": f"F{i}", "mimeType": "text/plain", "modifiedTime": "2026-10-01"}
        for i in range(4)
    ]
    fake.reply("GET", "/drive/v3/files", {"files": files})
    out = await _run(fake, "connectors__drive_search", {"query": "x", "limit": 2})
    q = fake.query()
    assert (q["pageSize"], q["fields"]) == (["2"], ["files(id,name,mimeType,modifiedTime)"])
    assert out.splitlines()[1:] == [
        "- F0 · text/plain · modified 2026-10-01 · id f0",
        "- F1 · text/plain · modified 2026-10-01 · id f1",
    ]


# --- whose Google ------------------------------------------------------------------------


async def test_a_run_for_no_one_has_no_google() -> None:
    fake = FakeGoogleApis()
    settings = _settings()
    await _connect(settings)
    async with serve(fake.responder) as base, _as(settings, person=""):
        out = await _tools(base)["connectors__calendar_list_events"].executor.execute({})
    assert fake.seen == []
    assert out.splitlines()[-1] == "this run acts for no one person, so there is no Google account to use"


async def test_someone_else_with_no_connection_is_told_so() -> None:
    fake = FakeGoogleApis()
    out = await _run(fake, "connectors__gmail_search", {"query": "x"}, person="felix-self|google:2")
    assert fake.seen == []
    assert (
        out.splitlines()[-1] == "the person has not connected a Google account; they can connect one in Felix"
    )


async def test_a_scope_the_person_did_not_grant_is_named() -> None:
    fake = FakeGoogleApis()
    settings = _settings()
    await _connect(settings, scopes=f"openid email {CAL}")
    out = await _run(
        fake,
        "connectors__gmail_draft",
        {"to": ["a@example.com"], "subject": "s", "body": "b"},
        settings=settings,
    )
    assert fake.seen == []
    assert out.splitlines()[-1] == (
        "me@example.com has not granted gmail_compose; the person can reconnect Google with it"
    )


async def test_two_accounts_need_naming_and_the_named_one_is_used() -> None:
    fake = FakeGoogleApis()
    fake.reply("GET", "/calendar/v3/calendars/primary/events", {"items": []})
    settings = _settings()
    await _connect(settings)
    await _connect(settings, account="2", label="work@example.com")
    unnamed = await _run(fake, "connectors__calendar_list_events", {}, settings=settings)
    assert unnamed.splitlines()[-1] == (
        "the person has connected several Google accounts (me@example.com, work@example.com); name one with `account`"
    )
    named = await _run(
        fake, "connectors__calendar_list_events", {"account": "me@example.com"}, settings=settings
    )
    assert named.splitlines()[-1] == "no events"
    assert len(fake.seen) == 1


async def test_a_revoked_connection_says_reconnect() -> None:
    from felix.auth import connections

    fake = FakeGoogleApis()
    settings = _settings()
    await _connect(settings)
    connections._memory[("acme", ME, "google", "1")]["status"] = "revoked"
    out = await _run(fake, "connectors__drive_search", {"query": "x"}, settings=settings)
    assert (
        out.splitlines()[-1]
        == "the Google connection for me@example.com no longer works; the person must reconnect it"
    )


@pytest.mark.parametrize(
    ("status", "code", "text"),
    [
        ("403 Forbidden", None, "Google refused: the connected account may lack the access this needs"),
        ("404 Not Found", None, "Google has nothing by that id for this account"),
        (
            "500 Internal Server Error",
            "provider_error",
            "[tool error/provider_error] Google Calendar answered 500",
        ),
    ],
)
async def test_what_google_refuses_is_told_in_words(status: str, code: str | None, text: str) -> None:
    fake = FakeGoogleApis()
    fake.reply(
        "GET",
        "/calendar/v3/calendars/primary/events",
        {"error": {"message": f"secret {TOKEN}"}},
        status=status,
    )
    out = await _run(fake, "connectors__calendar_list_events", {})
    got_code, content = _error(out)
    assert got_code == code
    assert content.splitlines()[-1] == text
    assert TOKEN not in content


# --- the production path -------------------------------------------------------------------


def test_the_tools_are_untrusted_and_only_the_writers_are_not_read_only() -> None:
    from felix.manifests.builder import _is_untrusted_tool
    from felix.tools.builtins import default_tool_provider
    from felix_connectors import gmail, google_calendar, google_drive

    provider = default_tool_provider()
    writers = {"connectors__calendar_create_event", "connectors__gmail_draft"}
    for family, hosts in (
        (google_calendar, {"www.googleapis.com"}),
        (gmail, {"gmail.googleapis.com"}),
        (google_drive, {"www.googleapis.com"}),
    ):
        for name in family.TOOL_NAMES:
            tool = provider.get(name)
            assert _is_untrusted_tool(tool), name
            assert tool.egress_hosts == hosts, name
            assert (tool.read_only, tool.replay_safe) == ((name not in writers),) * 2, name


def test_without_the_google_extra_a_tool_fails_by_the_extra_it_needs(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    from felix_connectors import gmail

    monkeypatch.setitem(sys.modules, "googleapiclient.discovery", None)
    with pytest.raises(ValueError, match=r"felix-connectors\[google\] extra"):
        gmail.build_tools(make_settings())


# --- more of Calendar and Gmail ------------------------------------------------------------


def test_an_all_day_event_reads_by_its_dates() -> None:
    from felix_connectors.google_calendar import event_line, render_events

    holiday = {
        "id": "e2",
        "summary": "Holiday",
        "start": {"date": "2026-11-26"},
        "end": {"date": "2026-11-27"},
    }
    assert event_line(holiday) == "2026-11-26 to 2026-11-27 · Holiday · id e2"
    assert render_events([]).splitlines() == ["source: Google Calendar", "no events"]


def test_a_busy_block_straddling_the_window_is_clipped_to_it() -> None:
    from felix_connectors.google_calendar import free_windows

    t = datetime(2026, 1, 1, 9, tzinfo=UTC)
    h = timedelta(hours=1)
    assert free_windows([(t - h, t + h), (t + 4 * h, t + 6 * h)], t, t + 5 * h, 30) == [(t + h, t + 4 * h)]


async def test_either_calendar_scope_lets_it_read_and_a_compose_grant_cannot_read_mail() -> None:
    fake = FakeGoogleApis()
    fake.reply("GET", "/calendar/v3/calendars/primary/events", {"items": []})
    settings = _settings()
    await _connect(settings, scopes=f"openid email {EVENTS} {COMPOSE}")
    listed = await _run(fake, "connectors__calendar_list_events", {}, settings=settings)
    assert listed.splitlines() == ["source: Google Calendar", "no events"]
    assert fake.seen[0].headers["user-agent"] == "felix-test"
    refused = await _run(fake, "connectors__gmail_search", {"query": "x"}, settings=settings)
    assert refused.splitlines()[-1] == (
        "me@example.com has not granted gmail_read; the person can reconnect Google with it"
    )


@pytest.mark.parametrize("account", ["2", "work@example.com"])
async def test_the_named_account_is_the_one_whose_token_is_sent(account: str) -> None:
    fake = FakeGoogleApis()
    fake.reply("GET", "/calendar/v3/calendars/primary/events", {"items": []})
    settings = _settings()
    await _connect(settings)
    await _connect(settings, account="2", label="work@example.com", token="ya29.work")
    await _run(fake, "connectors__calendar_list_events", {"account": account}, settings=settings)
    assert fake.seen[-1].headers["authorization"] == "Bearer ya29.work"


async def test_gmail_search_reads_no_more_than_the_limit() -> None:
    fake = FakeGoogleApis()
    fake.reply("GET", "/gmail/v1/users/me/messages", {"messages": [{"id": "m1"}, {"id": "m2"}, {"id": "m3"}]})
    fake.reply("GET", "/gmail/v1/users/me/messages/m1", MESSAGE)
    fake.reply("GET", "/gmail/v1/users/me/messages/m2", MESSAGE)
    out = await _run(fake, "connectors__gmail_search", {"query": "x", "limit": 2})
    assert len(fake.seen) == 3
    assert len([line for line in out.splitlines() if line.startswith("- ")]) == 2


NESTED = {
    "mimeType": "multipart/mixed",
    "parts": [
        {
            "mimeType": "multipart/alternative",
            "parts": [{"mimeType": "text/plain", "body": {"data": _b64("deep")}}],
        }
    ],
}


@pytest.mark.parametrize(
    ("payload", "last"),
    [
        (
            {"mimeType": "text/html", "body": {"data": _b64("<p>only html</p>")}, "parts": []},
            "(no plain-text body)",
        ),
        (NESTED, "deep"),
    ],
)
async def test_a_message_body_is_the_plain_text_wherever_it_is(payload: dict[str, Any], last: str) -> None:
    fake = FakeGoogleApis()
    fake.reply(
        "GET", "/gmail/v1/users/me/messages/m1", {**MESSAGE, "payload": {**MESSAGE["payload"], **payload}}
    )
    out = await _run(fake, "connectors__gmail_read", {"message_id": "m1"})
    assert out.splitlines()[-1] == last


async def test_a_draft_to_several_goes_to_each() -> None:
    from email.utils import getaddresses

    fake = FakeGoogleApis()
    fake.reply("POST", "/gmail/v1/users/me/drafts", {"id": "r1"})
    to = ["ana@example.com", "bo@example.com"]
    await _run(fake, "connectors__gmail_draft", {"to": to, "subject": "s", "body": "b"})
    assert fake.seen[0].headers["content-type"].startswith("application/json")
    raw = json.loads(fake.seen[0].body or b"{}")["message"]["raw"]
    mail = message_from_bytes(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    assert [a for _, a in getaddresses([mail["To"]])] == to


# --- the person's connection, as the vault answers ----------------------------------------


class _Refresher:
    name = "google"

    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.calls = 0

    async def refresh(self, settings: Any, refresh_token: str) -> Any:
        from felix.auth.connections import OAuthGrant

        self.calls += 1
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return OAuthGrant(access_token=self.outcome, expires_in=3600)

    async def revoke(self, settings: Any, token: str) -> None:
        return None


@pytest.fixture
def refresher(monkeypatch: pytest.MonkeyPatch) -> Any:
    from felix.auth import connections

    def use(outcome: Any) -> _Refresher:
        fake = _Refresher(outcome)
        monkeypatch.setitem(connections._PROVIDERS, "google", fake)
        return fake

    return use


async def _expired(settings: Any) -> None:
    from felix.auth import connections

    await _connect(settings)
    await connections._force_expiry_for_tests(settings, ("acme", ME, "google", "1"))


async def test_an_expired_token_is_refreshed_before_the_call(refresher: Any) -> None:
    fake = FakeGoogleApis()
    fake.reply("GET", "/calendar/v3/calendars/primary/events", {"items": []})
    google = refresher("ya29.fresh")
    settings = _settings()
    await _expired(settings)
    await _run(fake, "connectors__calendar_list_events", {}, settings=settings)
    await _run(fake, "connectors__calendar_list_events", {}, settings=settings)
    assert [r.headers["authorization"] for r in fake.seen] == ["Bearer ya29.fresh"] * 2
    assert google.calls == 1


async def test_a_refresh_google_refuses_tells_the_person_to_reconnect(refresher: Any) -> None:
    from felix.auth.connections import ConnectionRefused

    fake = FakeGoogleApis()
    refresher(ConnectionRefused("invalid_grant"))
    settings = _settings()
    await _expired(settings)
    out = await _run(fake, "connectors__gmail_search", {"query": "x"}, settings=settings)
    assert fake.seen == []
    assert out.splitlines()[-1] == "the person's Google connection no longer works; they must reconnect it"


async def test_a_refresh_outage_is_an_outage(refresher: Any, caplog: pytest.LogCaptureFixture) -> None:
    fake = FakeGoogleApis()
    refresher(RuntimeError(f"POST https://oauth2.googleapis.com/token refresh_token={TOKEN}"))
    settings = _settings()
    await _expired(settings)
    out = await _run(fake, "connectors__drive_search", {"query": "x"}, settings=settings)
    assert fake.seen == []
    assert _error(out) == (
        "transport_unavailable",
        "[tool error/transport_unavailable] Google Drive could not be reached just now",
    )
    assert TOKEN not in caplog.text


async def test_a_token_google_no_longer_accepts_says_reconnect() -> None:
    fake = FakeGoogleApis()
    fake.reply("GET", "/drive/v3/files", {"error": "invalid"}, status="401 Unauthorized")
    out = await _run(fake, "connectors__drive_search", {"query": "x"})
    assert (
        out.splitlines()[-1] == "Google no longer accepts this connection; the person may need to reconnect"
    )


# --- where requests go ---------------------------------------------------------------------


def test_the_production_endpoints_are_googles_own() -> None:
    from felix_connectors.google_common import GoogleApis

    apis = GoogleApis()
    calendar = apis.service("calendar", "v3").events().list(calendarId="primary").uri
    gmail = apis.service("gmail", "v1").users().messages().list(userId="me").uri
    drive = apis.service("drive", "v3").files().list().uri
    assert calendar.startswith("https://www.googleapis.com/calendar/v3/calendars/primary/events?")
    assert gmail.startswith("https://gmail.googleapis.com/gmail/v1/users/me/messages?")
    assert drive.startswith("https://www.googleapis.com/drive/v3/files?")
    assert (apis.googleapis_hosts, apis.gmail_hosts) == ({"www.googleapis.com"}, {"gmail.googleapis.com"})


def test_the_production_apis_carry_the_operators_terms() -> None:
    from felix_connectors.google_common import GoogleApis
    from felix_connectors.http import user_agent

    settings = make_settings(allow_insecure=False, connectors_contact="ops@example.com")
    apis = GoogleApis.from_settings(settings)
    assert (apis.user_agent, apis.allow_http) == (user_agent(settings), False)


async def test_each_family_goes_to_its_own_host_and_no_other() -> None:
    from felix_connectors.gmail import gmail_tools
    from felix_connectors.google_calendar import calendar_tools
    from felix_connectors.google_common import GoogleApis

    google, gmail = FakeGoogleApis(), FakeGoogleApis()
    google.reply("GET", "/calendar/v3/calendars/primary/events", {"items": []})
    gmail.reply("GET", "/gmail/v1/users/me/messages", {"messages": []})
    settings = _settings()
    await _connect(settings)
    async with serve(google.responder) as a, serve(gmail.responder) as b, _as(settings):
        apis = GoogleApis(googleapis=a, gmail=b, ua="felix-test", insecure=True)
        tools = {t.name: t for t in [*calendar_tools(apis), *gmail_tools(apis)]}
        await tools["connectors__calendar_list_events"].executor.execute({})
        await tools["connectors__gmail_search"].executor.execute({"query": "x"})
    assert [urlsplit(r.path).path for r in google.seen] == ["/calendar/v3/calendars/primary/events"]
    assert [urlsplit(r.path).path for r in gmail.seen] == ["/gmail/v1/users/me/messages"]


async def test_a_request_to_a_host_the_family_did_not_declare_is_refused() -> None:
    from felix_connectors.gmail import gmail_tools
    from felix_connectors.google_common import GoogleApis

    fake = FakeGoogleApis()
    settings = _settings()
    await _connect(settings)
    async with serve(fake.responder) as base, _as(settings):
        apis = GoogleApis(googleapis=base, gmail=base, ua="felix-test", insecure=True)
        apis.gmail_hosts = frozenset({"gmail.googleapis.com"})
        out = await gmail_tools(apis)[0].executor.execute({"query": "x"})
    assert fake.seen == []
    assert _error(out)[0] == "permission_denied"
