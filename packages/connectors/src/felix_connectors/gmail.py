"""Gmail: search the person's mail, read one message, save a draft.

Acts as the person through their vault (`google_common`). Searching and reading need
`gmail_read`; a draft needs only `gmail_compose`, which cannot read the inbox. A draft is never
sent -- there is no send tool -- and saving one changes the mailbox, so that tool is not
read-only. Every message is text anyone could have mailed the person: screen it.
"""

from __future__ import annotations

import base64
from email.message import EmailMessage
from functools import partial
from typing import Any

from felix.config import Settings
from felix.tools.types import Tool
from pydantic import Field

from felix_connectors.google_common import AccountArgs, GoogleApis, Header, Id, answered, capped, is_id
from felix_connectors.tools import connector_tool, one_line

SERVICE = "Gmail"
MAX_MESSAGES = 10
READ = ("gmail_read",)
COMPOSE = ("gmail_compose",)


def headers_of(message: dict[str, Any]) -> dict[str, str]:
    payload = message.get("payload")
    raw = payload.get("headers") if isinstance(payload, dict) else None
    return {str(h.get("name", "")).lower(): str(h.get("value", "")) for h in raw or [] if isinstance(h, dict)}


def message_line(message: dict[str, Any]) -> str:
    h = headers_of(message)
    return (
        f"- {one_line(h.get('date', '?'), 40)} · from {one_line(h.get('from', '?'), 120)} · "
        f"{one_line(h.get('subject', '(no subject)'), 160)} · id {one_line(message.get('id', ''), 64)}\n"
        f"  {one_line(message.get('snippet', ''), 240)}"
    )


def plain_text(part: Any) -> str:
    """The first text/plain body in a message's parts, decoded."""
    if not isinstance(part, dict):
        return ""
    body = part.get("body") if isinstance(part.get("body"), dict) else {}
    if part.get("mimeType") == "text/plain" and body.get("data"):
        data = str(body["data"])
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")
    for child in part.get("parts") or []:
        text = plain_text(child)
        if text:
            return text
    return ""


class SearchMailArgs(AccountArgs):
    query: str = Field(
        min_length=1, max_length=300, description="Gmail search, e.g. 'from:ana newer_than:7d'."
    )
    limit: int = Field(default=5, ge=1, le=MAX_MESSAGES)


class ReadMailArgs(AccountArgs):
    message_id: Id


class DraftArgs(AccountArgs):
    to: list[Header] = Field(min_length=1, max_length=20)
    subject: Header = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1, max_length=20_000)


#: Every tool `build_tools` returns, registered by name before any is built.
TOOL_NAMES = ("connectors__gmail_search", "connectors__gmail_read", "connectors__gmail_draft")


def build_tools(settings: Settings) -> list[Tool]:
    """The family's tools under `settings`; `ValueError` without the `google` extra."""
    return gmail_tools(GoogleApis.from_settings(settings))


def gmail_tools(apis: GoogleApis) -> list[Tool]:
    hosts = apis.gmail_hosts

    def users() -> Any:
        return apis.service("gmail", "v1").users()

    async def search(args: SearchMailArgs) -> str:
        listed = await apis.json(
            users().messages().list(userId="me", q=args.query, maxResults=args.limit),
            READ,
            args.account,
            hosts,
        )
        lines = [f"source: {SERVICE}"]
        for ref in (listed.get("messages") or [])[: args.limit]:
            mid = ref.get("id") if isinstance(ref, dict) else None
            if not is_id(mid):
                continue
            wanted = ["From", "Subject", "Date"]
            request = users().messages().get(userId="me", id=mid, format="metadata", metadataHeaders=wanted)
            lines.append(message_line(await apis.json(request, READ, args.account, hosts)))
        if len(lines) == 1:
            lines.append("no messages")
        return "\n".join(lines)

    async def read(args: ReadMailArgs) -> str:
        request = users().messages().get(userId="me", id=args.message_id, format="full")
        message = await apis.json(request, READ, args.account, hosts)
        h = headers_of(message)
        head = [f"{k}: {one_line(h.get(k, ''), 240)}" for k in ("from", "to", "date", "subject")]
        text = plain_text(message.get("payload")) or "(no plain-text body)"
        return "\n".join([f"source: {SERVICE}", *head, "", capped(text)])

    async def draft(args: DraftArgs) -> str:
        mail = EmailMessage()
        mail["To"] = ", ".join(args.to)
        mail["Subject"] = args.subject
        mail.set_content(args.body)
        raw = base64.urlsafe_b64encode(mail.as_bytes()).decode()
        request = users().drafts().create(userId="me", body={"message": {"raw": raw}})
        made = await apis.json(request, COMPOSE, args.account, hosts)
        return f"source: {SERVICE}\ndraft saved (not sent), id {one_line(made.get('id', ''), 64)}"

    tool = partial(connector_tool, family="gmail", service=SERVICE, hosts=hosts)
    return [
        tool(
            name="connectors__gmail_search",
            args=SearchMailArgs,
            run=answered(SERVICE, search),
            description="Search the person's Gmail: date, sender, subject and snippet of each match.",
        ),
        tool(
            name="connectors__gmail_read",
            args=ReadMailArgs,
            run=answered(SERVICE, read),
            description="One of the person's emails, by id, as plain text.",
        ),
        tool(
            name="connectors__gmail_draft",
            args=DraftArgs,
            run=answered(SERVICE, draft),
            read_only=False,
            description="Save an email draft in the person's Gmail. It is never sent.",
        ),
    ]


__all__ = ["TOOL_NAMES", "build_tools", "gmail_tools", "headers_of", "message_line", "plain_text"]
