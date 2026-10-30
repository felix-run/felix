"""`POST /connections/ics`: a person connects a published calendar feed by its link.

Called as the person, with a name for the feed ("Work") and the link their calendar service
published it at -- Outlook's "Publish a calendar" ICS link, Google Calendar's "Secret address in
iCal format", an iCloud public calendar's `webcal://` link. The link must be on a host
`FELIX_ICS_HOSTS` allows, and is fetched once and kept only if what it serves is a calendar,
sealed in the person's vault (provider `ics`, kind `secret`, account the name case folded) as
`https://`. Connecting a name again replaces its link; a person keeps at most `MAX_FEEDS`.
Listing and disconnecting are the vault's own routes (`GET /connections`,
`DELETE /connections/ics/{account}`); disconnecting forgets the link here, and the person
withdraws it at their calendar service, which is what stops it working for anyone who copied it.

The link is the credential: it is never logged, returned or put in an error. The body is read
here rather than by FastAPI, whose 422 echoes what it was sent, and a refusal is one of a few
written codes. Each person may try `ATTEMPTS` times an hour. Mounted by the plugin only with the
`ics` extra installed, and answering 404 where the vault has no sealing key.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from felix_connectors.connect import ConnectionView, refused
from felix_connectors.http import ResponseTooLarge, UpstreamError
from felix_connectors.ics import (
    MAX_FEEDS,
    MAX_LABEL,
    MAX_URL,
    PROVIDER,
    Feeds,
    HostNotAllowed,
    NotACalendar,
    account_of,
    check_calendar,
    check_feed_url,
    check_label,
)

logger = logging.getLogger("felix_connectors.ics_connect")

MOUNT = "/connections/ics"
ATTEMPTS = 10
WINDOW_S = 3600


class FeedConnect(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Bounded loosely: `check_label` holds the name to `MAX_LABEL` once its spaces are collapsed.
    label: str = Field(min_length=1, max_length=MAX_URL, description='A name for the feed, e.g. "Work".')
    url: SecretStr = Field(
        min_length=1,
        max_length=MAX_URL,
        description="The calendar's published ICS link (https:// or webcal://).",
    )


def _body(raw: bytes) -> tuple[str, str] | JSONResponse:
    """(name, link) from the body, the name checked, or the written refusal."""
    try:
        body = FeedConnect.model_validate(json.loads(raw))
    except ValueError, ValidationError:
        return refused("invalid_body", "send {label, url} as JSON")
    try:
        return check_label(body.label), body.url.get_secret_value()
    except ValueError:
        return refused(
            "label_invalid", f"a feed's name is 1 to {MAX_LABEL} printable characters, with no slash"
        )


async def _checked(feeds: Feeds, url: str) -> str | JSONResponse:
    """The link as kept -- `webcal://` as `https://`, trimmed -- when it serves a calendar from an
    allowed host, else the written refusal."""
    from felix.security.ssrf import EgressBlocked

    try:
        kept = check_feed_url(url, feeds.hosts, plain=feeds.allow_http)
        text = await feeds.fetch(kept)
        await asyncio.to_thread(check_calendar, text)  # a whole calendar: CPU, off the loop
    except HostNotAllowed:
        if not feeds.hosts:
            return refused("host_not_allowed", "this deployment reads no calendar feeds")
        allowed = ", ".join(sorted(feeds.hosts))
        return refused("host_not_allowed", f"this deployment reads feeds only from: {allowed}")
    except UpstreamError as exc:
        return refused("feed_refused", f"the calendar service answered {exc.status} for this link", 502)
    except ResponseTooLarge:
        return refused("feed_too_large", "this calendar is too large to read")
    except TimeoutError:
        return refused("feed_unavailable", "the calendar service did not answer in time; try again", 502)
    except EgressBlocked:
        return refused("host_not_allowed", "this link's host cannot be reached from this deployment")
    except NotACalendar:
        return refused("not_a_calendar", "this link does not serve an iCalendar calendar")
    except ValueError:
        # `check_feed_url`'s other refusals: written here, never the link.
        return refused("url_invalid", "send the calendar's https:// or webcal:// link")
    except Exception as exc:
        logger.warning("ics connect check failed error=%s", type(exc).__name__)
        return refused(
            "feed_unavailable", "the calendar service could not be reached just now; try again", 502
        )
    return kept


def build_router() -> APIRouter:
    router = APIRouter(tags=["Connections"])

    @router.post(
        "",
        response_model=ConnectionView,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": FeedConnect.model_json_schema()}},
            }
        },
    )
    async def connect_ics(request: Request) -> Any:
        """Connect a published calendar feed (an .ics link) by name."""
        from felix.auth import connections
        from felix.auth.mgmt import request_person

        settings = request.app.state.settings
        if not connections.store_enabled(settings):
            raise HTTPException(status_code=404, detail="not_found")
        tenant, person = request_person(request)
        if not await request.app.state.ics_limiter.hit(
            f"ics-connect:{tenant}:{person}", limit=ATTEMPTS, window_seconds=WINDOW_S
        ):
            return refused("rate_limited", "too many tries; wait an hour and try again", 429)
        parsed = _body(await request.body())
        if isinstance(parsed, JSONResponse):
            return parsed
        label, url = parsed
        account = account_of(label)
        try:
            theirs = await connections.list_for_person(settings, tenant, person)
        except connections.StoreUnavailable:
            return refused("connect_failed", "connecting did not finish; try again", 503)
        held = {c["account"] for c in theirs if c["provider"] == PROVIDER}
        if account not in held and len(held) >= MAX_FEEDS:
            return refused(
                "too_many_feeds", f"a person may connect {MAX_FEEDS} calendar feeds; disconnect one first"
            )
        feeds: Feeds = request.app.state.ics_feeds
        kept = await _checked(feeds, url)
        if isinstance(kept, JSONResponse):
            return kept
        try:
            stored = await connections.save_secret(
                settings, tenant, person=person, provider=PROVIDER, account=account, secret=kept, label=label
            )
        except connections.StoreUnavailable:
            return refused("connect_failed", "connecting did not finish; try again", 503)
        return ConnectionView.model_validate(connections.public_view(stored))

    return router


def mount(app: Any) -> None:
    """The route, when the `ics` extra is installed: without it there are no tools to use it."""
    from felix.security.rate_limit import build_rate_limiter_backend

    from felix_connectors.ics import require_parsers

    try:
        require_parsers()
    except ValueError:
        logger.info("ics connect not mounted: the felix-connectors[ics] extra is not installed")
        return
    settings = app.state.settings
    app.state.ics_feeds = Feeds.from_settings(settings)
    app.state.ics_limiter = build_rate_limiter_backend(settings)
    app.include_router(build_router(), prefix=MOUNT)


__all__ = ["ATTEMPTS", "MOUNT", "FeedConnect", "build_router", "mount"]
