"""Felix channels: talk to an agent from a chat platform. Slack today.

Installed, this plugin mounts `POST /channels/slack/{id}/events` for each Slack app the operator
registers in `FELIX_CHANNELS` (`config.py`). A message to the app -- a mention in a channel, or a
DM -- starts a durable run of the channel's manifest, as the person who sent it
(`slack:<team>:<user>`), on a thread for that Slack conversation, so a thread in Slack is a
conversation in Felix. When the run ends, the worker posts its answer back (`delivery.py`).

It also registers the `slack` destination kind (`destination.py`), so a scheduled agent's `post`
can publish into a Slack conversation through a registered app -- `FELIX_DESTINATIONS` entry
`{"kind": "slack", "channel": "<FELIX_CHANNELS id>", "conversation": "C…", "tenants": [...]}`,
the bot token the channel's own, for the channel's tenant only, sent plain so a model's
`<!channel>` or `@here` pings nobody and with unfurling off.

Core never names this package; it arrives through the `felix.plugins` entry point.
"""

from __future__ import annotations

from typing import Any

from felix_channels.routes import MOUNT, build_router


class ChannelsPlugin:
    name = "channels"

    def routes(self, app: Any, *, tools: Any) -> None:
        from felix.idempotency import build_idempotency_store
        from felix.security.rate_limit import build_rate_limiter_backend

        from felix_channels.config import parse_channels

        _ = tools
        settings = app.state.settings
        # Here, not at the first message: a malformed FELIX_CHANNELS fails the boot.
        parse_channels(settings)
        # Stores of the plugin's own, like the triggers' (`felix_api/app.py`): Slack's retries are
        # claimed apart from chat's Idempotency-Key traffic, and counted on their own window.
        app.state.channel_claims = build_idempotency_store(settings)
        app.state.channel_limiter = build_rate_limiter_backend(settings)
        app.include_router(build_router(), prefix=MOUNT)

    @property
    def self_authenticating_mounts(self) -> tuple[str, ...]:
        # Slack signs each request; the route verifies before it reads anything.
        return (MOUNT,)


async def _mask_secrets(app: Any) -> None:
    from felix_channels.config import mask_secrets

    await mask_secrets(app.state.settings)


def register(registry: Any) -> None:
    """Entry point, called once at startup in the API and the worker."""
    from felix_channels.delivery import on_run_finished
    from felix_channels.destination import KIND, SlackDestination
    from felix_channels.origin import PLUGIN

    registry.register_plugin(ChannelsPlugin())
    # `post` to a Slack conversation, in the API (the compile checks it) and the worker (where a
    # job's post is delivered).
    registry.register_destination_kind(KIND, SlackDestination())
    # Core hands this hook only the runs this plugin tagged with PLUGIN.
    registry.register_run_finished(on_run_finished, origin=PLUGIN)
    # The API resolves every channel secret at startup and registers it for masking. The worker --
    # where channel runs execute -- runs no plugin startup hooks: there the bot token is masked from
    # its first reply, unless FELIX_SECRET_NAMES names both secrets (resolved at every boot).
    registry.register_startup_hook(_mask_secrets)


__all__ = ["ChannelsPlugin", "register"]
