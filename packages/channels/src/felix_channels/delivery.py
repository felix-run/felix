"""The answer goes back: a `register_run_finished` hook, run in the worker when a run ends.

Core calls it only for runs this plugin tagged (`origin.PLUGIN`). A completed run posts its answer
to the Slack conversation it came from; one that failed, expired or died posts a short note
instead -- never the run's error text, which is the operator's to read.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from felix_channels.config import parse_channels, resolve_secret
from felix_channels.origin import ReplyTo
from felix_channels.slack import post_message

if TYPE_CHECKING:
    from felix.plugins import RunFinished

logger = logging.getLogger("felix_channels.delivery")

_NOTES = {
    "failed": "I couldn't finish that one.",
    "dead": "I couldn't finish that one.",
    "expired": "That took too long, and was stopped before I could answer.",
}


def reply_text(status: str, answer: str) -> str:
    """What the conversation is told for a run that ended with `status`."""
    if status == "completed":
        return answer.strip() or "I have no answer to that."
    return _NOTES.get(status, "I couldn't finish that one.")


async def on_run_finished(settings: Any, finished: RunFinished) -> None:
    reply = ReplyTo.from_origin(finished.origin)
    channel = parse_channels(settings).get(reply.channel)
    if channel is None or channel.tenant != finished.tenant_id:
        # Removed from FELIX_CHANNELS since the run started, or not this tenant's: nowhere to post.
        logger.warning("channel_reply_undeliverable run=%s", finished.run_id)
        return
    token = await resolve_secret(settings, channel.bot_token)
    text = reply_text(finished.status, finished.answer)
    if not await post_message(settings, token, reply.conversation, text, reply.thread_ts):
        logger.warning("channel_reply_refused channel=%s run=%s", channel.id, finished.run_id)


__all__ = ["on_run_finished", "reply_text"]
