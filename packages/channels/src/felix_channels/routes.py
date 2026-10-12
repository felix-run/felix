"""`POST /channels/slack/{id}/events`: Slack's Events API, and a durable run per message.

No bearer: Slack signs every request with the app's signing secret, and nothing in the body is
parsed before the signature verifies. An unknown id answers as a bad signature does. Slack wants
an answer within three seconds and retries otherwise, so the route only enqueues: each message is
claimed once by its `event_id` (a retry gets the first answer), checked against the channel's
allowlists and per-minute limit, and started as a durable run on the thread for its Slack
conversation, as `slack:<team>:<user>`. The answer goes back when the run ends, from the worker
(`delivery.py`).
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import JSONResponse
from felix.logging_setup import loggable

from felix_channels.config import MIN_SECRET_BYTES, SlackChannel, parse_channels, resolve_secret
from felix_channels.origin import ReplyTo
from felix_channels.slack import Message, SignatureRejected, message_from, post_message, verify

logger = logging.getLogger("felix_channels.routes")

MOUNT = "/channels"
#: Slack's event payloads are a few KiB; anything near this is not one.
MAX_BODY_BYTES = 64 * 1024
WINDOW_S = 60
BUSY = "I'm still working on the last message in this conversation. Send this again once I've answered."
UNAVAILABLE = "This agent isn't available here. Ask whoever set it up to check its configuration."


class _Unavailable(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _Busy(Exception):
    """The conversation's thread already has a run in flight."""

    def __init__(self, thread: str) -> None:
        super().__init__(thread)
        self.thread = thread


class _Refused(Exception):
    """The manifest's inbound auth refused the channel's principal. Nothing in that decision is the
    sender's own -- every sender has the channel's scheme and scopes -- so it refuses everyone: the
    channel is misconfigured (its `scopes`, or the manifest's `auth.inbound`)."""


class _RateLimited(Exception):
    pass


def build_router() -> APIRouter:
    # Out of the OpenAPI document: Slack is the only caller, and the checked-in wire contract
    # (`schemas/openapi.json`) is core's, the same with or without this plugin installed.
    router = APIRouter(tags=["Channels"], include_in_schema=False)

    @router.post("/slack/{channel_id}/events")
    async def slack_events(channel_id: str, request: Request, background: BackgroundTasks) -> Any:
        return await _slack_events(channel_id, request, background)

    return router


async def _slack_events(channel_id: str, request: Request, background: BackgroundTasks) -> Any:
    from felix.idempotency import IdempotencyConflict, StoredResponse, once

    settings = request.app.state.settings
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="payload_too_large")
    channel = await _verified(settings, channel_id, request, body)
    try:
        payload = json.loads(body)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid_payload") from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="invalid_payload")
    if payload.get("type") == "url_verification":
        # The handshake Slack sends when the request URL is saved, signed like any event.
        challenge = payload.get("challenge")
        if not isinstance(challenge, str) or len(challenge) > 256:
            raise HTTPException(status_code=400, detail="invalid_payload")
        return {"challenge": challenge}
    message = message_from(payload)
    if (
        message is None
        or message.team != channel.team_id
        or not channel.admits(message.channel, message.user)
    ):
        return {"ok": True}

    async def handle() -> StoredResponse:
        limiter = request.app.state.channel_limiter
        # Per sender first, and lower, so one person cannot spend the whole workspace's budget; a
        # sender refused there spends nothing of the channel's.
        limits = (
            (f"channel:{channel.id}:{message.team}:{message.user}", channel.user_rate_limit),
            (f"channel:{channel.id}", channel.rate_limit),
        )
        for key, limit in limits:
            if not await limiter.hit(key, limit=limit, window_seconds=WINDOW_S):
                raise _RateLimited
        try:
            await _start_run(settings, channel, message)
        except _Busy as busy:
            # Once a minute per conversation, however often someone writes into it meanwhile.
            if await limiter.hit(f"busy:{busy.thread}", limit=1, window_seconds=WINDOW_S):
                background.add_task(_notice, settings, channel, message, BUSY)
        except _Refused:
            # The channel's own configuration, so a retry would fare the same: acknowledged, not
            # retried -- but said, to the operator and (once a minute) in Slack, never silently.
            logger.warning("channel_misconfigured channel=%s reason=inbound_auth", channel.id)
            if await limiter.hit(f"refused:{channel.id}", limit=1, window_seconds=WINDOW_S):
                background.add_task(_notice, settings, channel, message, UNAVAILABLE)
        return StoredResponse(status=200, body={"ok": True})

    try:
        response, _ = await once(
            request.app.state.channel_claims,
            f"slack:{channel.id}",
            message.event_id,
            hashlib.sha256(body).hexdigest(),
            handle,
        )
    except IdempotencyConflict as exc:
        if exc.kind != "in_progress":
            # The same signed event_id with another body: not a retry Slack would send.
            logger.warning("channel_event_id_reused channel=%s", channel.id)
        # Either way Slack is told it arrived: a retry of an event still being handled, or one
        # nothing will be done with.
        return {"ok": True}
    except _RateLimited:
        raise HTTPException(status_code=429, detail="rate_limited") from None
    except _Unavailable as exc:
        logger.warning("channel_unavailable channel=%s reason=%s", channel.id, exc.reason)
        raise HTTPException(status_code=503, detail="channel_unavailable") from None
    return JSONResponse(response.body, status_code=response.status)


async def _verified(settings: Any, channel_id: str, request: Request, body: bytes) -> SlackChannel:
    """The channel `channel_id` names, once Slack's signature over `body` verifies under its
    secret. An unknown id answers as a bad signature does, so ids cannot be probed."""
    from felix.triggers import now_s

    channel = parse_channels(settings).get(channel_id)
    try:
        if channel is None:
            raise SignatureRejected("unknown_channel")
        try:
            secret = await resolve_secret(settings, channel.signing_secret)
        except Exception:
            secret = ""
        if len(secret.encode()) < MIN_SECRET_BYTES:
            # Unresolvable, or resolved empty or short (an unset variable behind secret:NAME):
            # the channel is broken, not the request -- and an empty key verifies nothing.
            logger.warning("channel_unavailable channel=%s reason=secret", channel.id)
            raise HTTPException(status_code=503, detail="channel_unavailable")
        verify(secret, request.headers, body, now_s())
    except SignatureRejected as exc:
        logger.info("channel_rejected channel=%s reason=%s", loggable(channel_id, limit=64), exc.code)
        raise HTTPException(status_code=401, detail="invalid_signature") from None
    return channel


async def _start_run(settings: Any, channel: SlackChannel, message: Message) -> str:
    """The run for `message`, as its sender, on its conversation's thread; its id."""
    from felix.context import AuthContext, RequestContext, async_run_with_context
    from felix.durability.fibers import RunInProgress
    from felix.durability.runs import start_durable_chat
    from felix.manifests.inbound_auth import InboundAuthError
    from felix.manifests.pin import pin_fields_for
    from felix.runtime import prepare_tenant_invoke, resolve_tenant_manifest
    from felix.thread_ids import channel_thread_id
    from felix_ai.types import ChatMessage

    thread = channel_thread_id(channel.tenant, channel.id, message.channel, message.thread_key)
    if thread is None:
        raise _Unavailable("the conversation does not make a thread id")
    auth = AuthContext(
        principal_sub=f"slack:{message.team}:{message.user}",
        tenant_id=channel.tenant,
        scopes=channel.scopes,
        anonymous=False,
        scheme="slack",
    )
    ctx = RequestContext(settings=settings, auth=auth, manifest_id=channel.manifest, thread_id=thread)
    # As the channel's tenant throughout: row-level security reads the ambient one.
    async with async_run_with_context(ctx):
        try:
            resolved = await resolve_tenant_manifest(
                settings, channel.tenant, channel.manifest, thread_id=thread
            )
        except Exception as exc:
            raise _Unavailable(f"manifest {channel.manifest!r} did not resolve") from exc
        try:
            await prepare_tenant_invoke(settings, resolved=resolved, auth=auth, thread_id=thread)
        except InboundAuthError as exc:
            raise _Refused("inbound auth") from exc
        except Exception as exc:
            raise _Unavailable("the run could not be admitted") from exc
        origin = ReplyTo(channel.id, message.channel, message.reply_thread_ts).to_origin()
        try:
            started = await start_durable_chat(
                settings,
                channel.tenant,
                manifest_id=channel.manifest,
                messages=[ChatMessage(role="user", content=message.text)],
                thread_id=thread,
                model_id=None,
                execution=resolved.manifest.spec.execution,
                pin=await pin_fields_for(
                    settings, channel.tenant, resolved.manifest, version=resolved.version
                ),
                origin=origin,
            )
        except RunInProgress:
            raise _Busy(thread) from None
        except Exception as exc:
            raise _Unavailable("the run could not be enqueued") from exc
    return str(started["resume_token"])


async def _notice(settings: Any, channel: SlackChannel, message: Message, text: str) -> None:
    """A short reply from Felix itself, best effort."""
    try:
        token = await resolve_secret(settings, channel.bot_token)
        if not await post_message(settings, token, message.channel, text, message.reply_thread_ts):
            logger.warning("channel_notice_refused channel=%s", channel.id)
    except Exception:
        logger.warning("channel_notice_failed channel=%s", channel.id, exc_info=True)


__all__ = ["BUSY", "MOUNT", "UNAVAILABLE", "build_router"]
