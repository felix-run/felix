"""The `slack` destination kind: `post` publishing into a Slack conversation through a channel's app.

    FELIX_DESTINATIONS={"team-brief": {"kind": "slack", "channel": "acme-slack",
                                       "conversation": "C0123ABCD", "tenants": ["acme"]}}

`channel` names a `FELIX_CHANNELS` entry (`config.py`): the Slack app, its workspace and its bot
token. The destination holds no secret of its own -- the token is the channel's, resolved when a
post is delivered and registered for masking then, since the worker that runs a job's post runs no
plugin startup hooks (`__init__._mask_secrets` covers the API only). A Slack app is bound to one
tenant, so a destination posts through it only for that tenant: the compile is refused for any
other, with the words an unregistered channel gets, so no tenant learns another's configuration.
`conversation` is the Slack id of where the post goes -- a channel `C…`, a private channel `G…`, a
DM `D…`.

What reaches Slack is the post as the harness cleaned it (secrets redacted, links as the
registration says, screened), then escaped and sent plain (`slack.post_classified`): `parse: none`
and `link_names: false`, so `<!channel>`, `<!here>`, `<!everyone>`, `<@U…>` and `@here` arrive as
text and ping nobody, and unfurling off, so a link the registration keeps is not fetched by
Slack's previews either. A title leads the text, bold.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from felix.destinations import Delivery, Destination, Post

    from felix_channels.config import SlackChannel

logger = logging.getLogger("felix_channels.destination")

KIND = "slack"
# Where a post may go: a channel, a private channel, or a DM, by Slack id.
CONVERSATION_RE = re.compile(r"[CGD][A-Z0-9]{2,31}\Z")


def _channel(settings: Any, tenant_id: str, dest: Destination) -> SlackChannel | None:
    """The `FELIX_CHANNELS` entry `dest` posts through, when it is registered for `tenant_id`."""
    from felix_channels.config import parse_channels

    name = dest.fields.get("channel")
    if not isinstance(name, str) or not name:
        return None
    try:
        channel = parse_channels(settings).get(name)
    except ValueError:
        # A malformed FELIX_CHANNELS fails the API's boot; here it is simply not a channel.
        logger.warning("destination_channels_unparsed destination=%s", dest.name)
        return None
    return channel if channel is not None and channel.tenant == tenant_id else None


def message_text(post: Post) -> str:
    """The text Slack is sent, before `slack` escapes it: the title, bold and on one line, then
    the body. The title's own `*` are dropped so it cannot close the bold early."""
    title = " ".join(post.title.split()).replace("*", "")
    return f"*{title}*\n{post.text}" if title else post.text


class SlackDestination:
    """`kind: slack`. Fields: `channel` (a `FELIX_CHANNELS` id of the compiling tenant) and
    `conversation` (a Slack channel, private channel or DM id)."""

    def validate(self, settings: Any, tenant_id: str, dest: Destination) -> None:
        from felix.destinations import DestinationError

        if not isinstance(dest.fields.get("channel"), str) or not dest.fields.get("channel"):
            raise DestinationError(f"destination {dest.name} (slack) needs a `channel`")
        conversation = dest.fields.get("conversation")
        if not isinstance(conversation, str) or not CONVERSATION_RE.match(conversation):
            raise DestinationError(
                f"destination {dest.name} (slack) needs a `conversation`: a Slack channel, private "
                "channel or DM id (C…, G… or D…)"
            )
        if _channel(settings, tenant_id, dest) is None:
            # The channel id is the operator's and is not repeated: whether it exists, or for which
            # tenant, is not this tenant's to learn.
            raise DestinationError(
                f"destination {dest.name} names a Slack channel not registered for this tenant"
            )

    async def secrets(self, settings: Any, tenant_id: str, dest: Destination) -> list[str]:
        """The bot token, resolved (and registered for masking) before `post` cleans the text it
        sends (`felix.destinations.DestinationSecrets`): in a worker, nothing else has registered it
        yet. None for a channel that is not this tenant's; `deliver` refuses that."""
        from felix_channels.config import resolve_secret

        channel = _channel(settings, tenant_id, dest)
        return [] if channel is None else [await resolve_secret(settings, channel.bot_token)]

    async def deliver(self, settings: Any, tenant_id: str, dest: Destination, post: Post) -> Delivery:
        from felix.destinations import Delivery

        from felix_channels.config import resolve_secret
        from felix_channels.slack import post_classified

        channel = _channel(settings, tenant_id, dest)
        conversation = dest.fields.get("conversation")
        if channel is None or not isinstance(conversation, str) or not CONVERSATION_RE.match(conversation):
            return Delivery("not_posted", detail="the destination's Slack channel is no longer registered")
        try:
            # Registered for masking as it resolves (`config.resolve_secret`).
            token = await resolve_secret(settings, channel.bot_token)
        except Exception:
            logger.warning("destination_token_unresolved destination=%s channel=%s", dest.name, channel.id)
            token = ""
        if not token:
            return Delivery("not_posted", detail="the Slack app's token could not be resolved")
        from felix.secrets import redact_text

        # Again here, with the token in hand: whatever cleaned the text, it never reaches Slack.
        text = redact_text(message_text(post), [token])
        delivery = await post_classified(settings, token, conversation, text)
        if delivery.status != "posted":
            logger.warning(
                "destination_slack_%s destination=%s channel=%s", delivery.status, dest.name, channel.id
            )
        return delivery


__all__ = ["CONVERSATION_RE", "KIND", "SlackDestination", "message_text"]
