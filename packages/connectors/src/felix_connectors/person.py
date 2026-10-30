"""What every family acting as a person shares: whose connection, and how a refusal reads.

The Google and iCloud tools act for the person the run acts for (`AuthContext.person`), with a
connection that person keeps in their vault (`felix.auth.connections`): never an operator's
credential, never another person's. A run that acts for no one person -- an anonymous caller, a
shared API key, a trigger -- has no account to use, and the tool says so in words.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from felix_connectors.tools import one_line

#: What one mail body, file or note contributes to an answer.
MAX_TEXT = 8000


class Unavailable(Exception):
    """Something the model should be told in words and not retry around: the person has no
    account connected, it lacks a scope, they named an account they do not have."""


@dataclass(frozen=True, slots=True)
class PersonConnection:
    """The one connection a call acts through: whose, in which tenant, and its vault row."""

    settings: Any
    tenant: str
    person: str
    provider: str
    service: str
    row: dict[str, Any]

    @property
    def account(self) -> str:
        return self.row["account"]

    async def opened(self, fetch: Callable[..., Awaitable[str]]) -> str:
        """`fetch` (`connections.access_token` or `connections.secret`) for this connection, with
        the vault's refusals answered in words, worded once for every family."""
        from felix.auth import connections

        try:
            return await fetch(self.settings, self.tenant, self.person, self.provider, self.account)
        except connections.ConnectionRevoked:
            raise Unavailable(
                f"the person's {self.service} connection no longer works; they must reconnect it"
            ) from None
        except connections.NotConnected, connections.AmbiguousConnection:
            raise Unavailable(f"the person's {self.service} connection changed just now; try again") from None
        except connections.ProviderUnavailable:
            raise Unavailable(f"this deployment cannot refresh {self.service} connections") from None


async def person_connections(provider: str, service: str, noun: str = "") -> list[PersonConnection]:
    """Every `provider` connection of the person this run acts for, working or not, for a family
    that reads several at once (calendar feeds). `Unavailable` when the run acts for no one person,
    worded with `noun` (default "`service` account")."""
    from felix.auth import connections
    from felix.context import try_get_context

    noun = noun or f"{service} account"
    ctx = try_get_context()
    person = ctx.auth.person if ctx is not None else ""
    if ctx is None or not person or not ctx.auth.tenant_id:
        raise Unavailable(f"this run acts for no one person, so there is no {noun} to use")
    return [
        PersonConnection(ctx.settings, ctx.auth.tenant_id, person, provider, service, c)
        for c in await connections.list_for_person(ctx.settings, ctx.auth.tenant_id, person)
        if c["provider"] == provider
    ]


async def person_connection(provider: str, service: str, account: str | None) -> PersonConnection:
    """The one `provider` connection of the person this run acts for -- the one named `account`
    (its label or id) when they have several, and active. `Unavailable`, worded with `service`
    ("Google"), otherwise."""
    noun = f"{service} account"
    article = "an" if service[:1].lower() in "aeiou" else "a"
    mine = await person_connections(provider, service)
    if account:
        mine = [c for c in mine if account in (c.row["label"], c.account)]
        if not mine:
            raise Unavailable(f"the person has not connected the {noun} {one_line(account, 120)}")
    if not mine:
        raise Unavailable(f"the person has not connected {article} {noun}; they can connect one in Felix")
    if len(mine) > 1:
        labels = ", ".join(one_line(c.row["label"], 120) for c in mine)
        raise Unavailable(f"the person has connected several {noun}s ({labels}); name one with `account`")
    (found,) = mine
    if found.row["status"] != "active":
        raise Unavailable(
            f"the {service} connection for {one_line(found.row['label'], 120)} no longer works; "
            "the person must reconnect it"
        )
    return found


def event_key(person: str, summary: str, start: datetime, end: datetime) -> bytes:
    """What makes two created events the same one, for every calendar family: whose, what, when.
    Each family encodes a digest of it as its provider's id allows; a retry after a timeout then
    names the event it already made, and the provider refuses it twice."""
    return hashlib.sha256(
        "\x1f".join([person, summary, start.isoformat(), end.isoformat()]).encode()
    ).digest()


def answered(service: str, fn: Callable[[Any], Awaitable[str]]) -> Callable[[Any], Awaitable[str]]:
    """`fn`, with an `Unavailable` answered as a finding rather than an outage to retry."""

    async def run(args: Any) -> str:
        try:
            return await fn(args)
        except Unavailable as exc:
            return f"source: {service}\n{exc}"

    return run


def capped(text: str) -> str:
    """Line breaks kept -- a mail or a document reads in paragraphs -- control characters not."""
    clean = "".join(ch if ch.isprintable() or ch in "\n\t" else " " for ch in text)
    return clean[:MAX_TEXT] + "\n(… cut)" if len(clean) > MAX_TEXT else clean


__all__ = [
    "MAX_TEXT",
    "PersonConnection",
    "Unavailable",
    "answered",
    "capped",
    "event_key",
    "person_connection",
    "person_connections",
]
