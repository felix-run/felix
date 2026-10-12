"""Slack's wire: request signatures, the events Felix answers, and `chat.postMessage`.

Signature (https://api.slack.com/authentication/verifying-requests-from-slack):
`X-Slack-Signature: v0=<hex HMAC-SHA256 of "v0:{X-Slack-Request-Timestamp}:{body}">` under the app's
signing secret, with the timestamp within five minutes either way.

What starts a run: an `app_mention` in a channel, answered in a thread under it, and a message in
the app's DM. Edits, deletions, bot messages (this app's own included) and every other subtype
are ignored, so the app never answers itself. So is a sender from another organisation in a Slack
Connect channel: the outer `team_id` is the installing workspace, the sender's own is
`event.user_team`, and only the bound workspace's people may talk to its agent.

What goes back is escaped as Slack asks (`&`, `<`, `>`), so an answer cannot ping `<!channel>` or
dress a link as something else, and is posted with unfurling off, so Slack itself fetches no URL
the model wrote.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from felix.security.constant_time import constant_time_equal

from felix_channels.config import MIN_SECRET_BYTES, SLACK_ID_RE

API_BASE = "https://slack.com/api"
TIMESTAMP_TOLERANCE_S = 300
#: Slack refuses `text` past 40,000 characters; past this, the answer is cut and says so.
MAX_TEXT = 39_000
#: One `chat.postMessage`, end to end: inside the worker's per-hook bound, which holds a slot.
POST_TIMEOUT_S = 8.0
# A message timestamp, Slack's id for a message and for the thread under it.
_TS_RE = re.compile(r"[0-9]{1,12}\.[0-9]{1,9}\Z")
_EVENT_ID_RE = re.compile(r"Ev[A-Z0-9]{4,32}\Z")
_MENTION_RE = re.compile(r"<@[A-Z0-9]+>")
_PARTIAL_ENTITY_RE = re.compile(r"&[a-z]{0,3}\Z")


class SignatureRejected(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def verify(signing_secret: str, headers: Mapping[str, str], body: bytes, now: int) -> None:
    """Raise `SignatureRejected` unless Slack signed `body` with `signing_secret`, recently."""
    if len(signing_secret.encode()) < MIN_SECRET_BYTES:
        # An empty secret (an unset variable behind secret:NAME) would verify an empty-key HMAC.
        raise SignatureRejected("signing_secret_too_short")
    raw_ts = headers.get("x-slack-request-timestamp") or ""
    if not (raw_ts.isascii() and raw_ts.isdigit()) or len(raw_ts) > 12:
        raise SignatureRejected("missing_or_malformed_timestamp")
    if abs(now - int(raw_ts)) > TIMESTAMP_TOLERANCE_S:
        raise SignatureRejected("timestamp_out_of_tolerance")
    offered = headers.get("x-slack-signature") or ""
    expected = (
        "v0=" + hmac.new(signing_secret.encode(), f"v0:{raw_ts}:".encode() + body, hashlib.sha256).hexdigest()
    )
    if not offered or not constant_time_equal(expected, offered):
        raise SignatureRejected("signature_mismatch")


@dataclass(frozen=True, slots=True)
class Message:
    """A Slack message Felix answers: who, where, and the thread its reply goes under (None for
    a DM answered in place)."""

    event_id: str
    team: str
    user: str
    channel: str
    text: str
    # The conversation's key within the channel: the thread's root ts, or "dm" for a DM's main line.
    thread_key: str
    reply_thread_ts: str | None


def message_from(payload: Mapping[str, Any]) -> Message | None:
    """The message an `event_callback` carries, or None for anything Felix does not answer."""
    if payload.get("type") != "event_callback":
        return None
    event = payload.get("event")
    if not isinstance(event, dict):
        return None
    kind = event.get("type")
    is_dm = kind == "message" and event.get("channel_type") == "im"
    if kind != "app_mention" and not is_dm:
        return None
    if event.get("subtype") or event.get("bot_id"):
        # Edits, deletions, joins, and any bot's own messages -- this app's replies among them.
        return None
    event_id, team = payload.get("event_id"), payload.get("team_id")
    for home in (event.get("user_team"), event.get("source_team")):
        if home is not None and home != team:
            # Slack Connect: a person from another organisation, in a channel shared with it. (A
            # sibling workspace of the same Enterprise Grid org is another workspace too.)
            return None
    if payload.get("is_ext_shared_channel") and event.get("user_team") is None:
        # A shared channel whose sender's workspace Slack did not say: not shown to be ours.
        return None
    user, channel, ts = event.get("user"), event.get("channel"), event.get("ts")
    thread_ts = event.get("thread_ts")
    if not (
        isinstance(event_id, str) and _EVENT_ID_RE.match(event_id)
        and all(isinstance(v, str) and SLACK_ID_RE.match(v) for v in (team, user, channel))
        and isinstance(ts, str) and _TS_RE.match(ts)
        and (thread_ts is None or (isinstance(thread_ts, str) and _TS_RE.match(thread_ts)))
    ):  # fmt: skip
        return None
    text = _MENTION_RE.sub("", str(event.get("text") or "")).strip()
    if not text:
        return None
    if is_dm and thread_ts is None:
        # A DM's main line is one running conversation, answered in place.
        thread_key, reply = "dm", None
    else:
        # A mention starts (or continues) a thread under the message; so does a DM thread.
        thread_key = reply = thread_ts or ts
    return Message(
        event_id=event_id,
        team=str(team),
        user=str(user),
        channel=str(channel),
        text=text,
        thread_key=thread_key,
        reply_thread_ts=reply,
    )


def slack_http_client(settings: Any) -> Any:
    """The client Slack's API is called with: the egress guard's, like every outbound call.
    Module-level, so a test can hand in a fake."""
    from felix.security.egress import safe_async_client

    _ = settings
    return safe_async_client(timeout=10.0)


def escape(text: str) -> str:
    """Slack's own escaping for message text: what remains cannot be read as a control sequence."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def post_message(settings: Any, token: str, channel: str, text: str, thread_ts: str | None) -> bool:
    """`chat.postMessage`: `text` escaped, cut to `MAX_TEXT`, unfurling off, the whole call bounded
    by `POST_TIMEOUT_S`. True when Slack took it; a refusal is logged by the caller, never the
    token."""
    text = escape(text)
    if len(text) > MAX_TEXT:
        # Not mid-entity: a cut `&amp;` would read as a stray `&am`.
        text = (
            _PARTIAL_ENTITY_RE.sub("", text[:MAX_TEXT]) + "\n\n…(cut: the answer was longer than Slack shows)"
        )
    body: dict[str, Any] = {
        "channel": channel,
        "text": text,
        # Plain text, whatever Slack's defaults become: no `@here` expanded into a ping, no link
        # unfurled -- Slack fetches no URL the model wrote.
        "parse": "none",
        "link_names": False,
        "unfurl_links": False,
        "unfurl_media": False,
    }
    if thread_ts:
        body["thread_ts"] = thread_ts
    async with asyncio.timeout(POST_TIMEOUT_S), slack_http_client(settings) as client:
        resp = await client.post(
            f"{API_BASE}/chat.postMessage", json=body, headers={"Authorization": f"Bearer {token}"}
        )
    try:
        data = resp.json()
    except ValueError:
        return False
    return resp.status_code == 200 and isinstance(data, dict) and data.get("ok") is True


__all__ = [
    "API_BASE",
    "MAX_TEXT",
    "Message",
    "SignatureRejected",
    "escape",
    "message_from",
    "post_message",
    "slack_http_client",
    "verify",
]
