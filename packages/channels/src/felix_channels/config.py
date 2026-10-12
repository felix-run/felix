"""`FELIX_CHANNELS`: the operator's map of chat-platform connections.

    {"acme-slack": {"platform": "slack", "tenant": "acme", "manifest": "assistant",
                    "team_id": "T0123ABCD", "signing_secret": "secret:SLACK_SIGNING_SECRET",
                    "bot_token": "secret:SLACK_BOT_TOKEN", "scopes": [],
                    "channels": ["C0123ABCD"], "users": ["U0123ABCD"], "rate_limit": 60}}

One entry is one Slack app in one workspace, bound to one tenant and one manifest. Everyone who
can message the app there talks to that agent as `slack:<team>:<user>`, with `scopes` (none by
default); `channels` and `users` narrow who may, when set. Secrets are `secret:NAME` except on a
development install, as for `FELIX_TRIGGERS`.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from felix.settings_registry import development_install, parse_registry

logger = logging.getLogger("felix_channels.config")

#: The shortest signing secret accepted, as written or as resolved: an empty one is a signature
#: anyone can compute. Slack's are 32 hex characters.
MIN_SECRET_BYTES = 16
DEFAULT_RATE_LIMIT = 60
#: Per sender, below the channel's own, so one person cannot spend the whole workspace's budget.
DEFAULT_USER_RATE_LIMIT = 10
MAX_RATE_LIMIT = 10_000
# Slack's ids: a team `T…`, channels `C…`/`G…`/`D…`, users `U…`/`W…`. Validated, since each
# reaches a thread id, a log line and an API call.
SLACK_ID_RE = re.compile(r"[A-Z0-9]{2,32}\Z")
_KEYS = frozenset(
    {"platform", "tenant", "manifest", "team_id", "signing_secret", "bot_token"}
    | {"scopes", "channels", "users", "rate_limit", "user_rate_limit"}
)


@dataclass(frozen=True, slots=True)
class SlackChannel:
    """One `FELIX_CHANNELS` entry for Slack, validated."""

    id: str
    tenant: str
    manifest: str
    team_id: str
    signing_secret: str
    bot_token: str
    scopes: frozenset[str]
    # None for anyone in the workspace.
    channels: frozenset[str] | None
    users: frozenset[str] | None
    rate_limit: int
    # Attempts a minute for one sender; at most `rate_limit`.
    user_rate_limit: int

    def admits(self, channel: str, user: str) -> bool:
        return (self.channels is None or channel in self.channels) and (
            self.users is None or user in self.users
        )


def _ids(spec: dict[str, Any], key: str, *, name: str) -> frozenset[str] | None:
    value = spec.get(key)
    if value is None:
        return None
    if not (
        isinstance(value, list) and value and all(isinstance(v, str) and SLACK_ID_RE.match(v) for v in value)
    ):
        raise ValueError(f"channel {name!r} `{key}` must be a non-empty list of Slack ids")
    return frozenset(value)


def _secret(spec: dict[str, Any], key: str, *, name: str, allow_literal: bool) -> str:
    from felix.secrets import is_secret_ref

    value = spec.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"channel {name!r} needs `{key}` (secret:NAME)")
    if not is_secret_ref(value):
        if not allow_literal:
            raise ValueError(f"channel {name!r} `{key}` must be secret:NAME outside development")
        if len(value.encode()) < MIN_SECRET_BYTES:
            raise ValueError(f"channel {name!r} `{key}` must be at least {MIN_SECRET_BYTES} bytes")
    return value


def _parse_one(name: str, spec: dict[str, Any], *, allow_literal: bool) -> SlackChannel:
    from felix.auth.context import assert_valid_tenant_id

    unknown = set(spec) - _KEYS
    if unknown:
        raise ValueError(f"channel {name!r} has unknown keys: {', '.join(sorted(unknown))}")
    if spec.get("platform") != "slack":
        raise ValueError(f"channel {name!r} `platform` must be slack")
    for key in ("tenant", "manifest"):
        if not isinstance(spec.get(key), str) or not spec[key].strip():
            raise ValueError(f"channel {name!r} needs `{key}`")
    try:
        assert_valid_tenant_id(spec["tenant"])
    except ValueError as exc:
        raise ValueError(f"channel {name!r} `tenant`: {exc}") from None
    team = spec.get("team_id")
    if not isinstance(team, str) or not SLACK_ID_RE.match(team):
        raise ValueError(f"channel {name!r} `team_id` must be a Slack team id (T…)")
    scopes = spec.get("scopes", [])
    if not (isinstance(scopes, list) and all(isinstance(s, str) and s for s in scopes)):
        raise ValueError(f"channel {name!r} `scopes` must be a list of non-empty strings")
    rate_limit = spec.get("rate_limit", DEFAULT_RATE_LIMIT)
    if isinstance(rate_limit, bool) or not (
        isinstance(rate_limit, int) and 1 <= rate_limit <= MAX_RATE_LIMIT
    ):
        raise ValueError(f"channel {name!r} `rate_limit` must be a whole number from 1 to {MAX_RATE_LIMIT}")
    user_rate_limit = spec.get("user_rate_limit", min(DEFAULT_USER_RATE_LIMIT, rate_limit))
    if isinstance(user_rate_limit, bool) or not (
        isinstance(user_rate_limit, int) and 1 <= user_rate_limit <= rate_limit
    ):
        raise ValueError(f"channel {name!r} `user_rate_limit` must be a whole number from 1 to `rate_limit`")
    return SlackChannel(
        id=name,
        tenant=spec["tenant"],
        manifest=spec["manifest"].strip(),
        team_id=team,
        signing_secret=_secret(spec, "signing_secret", name=name, allow_literal=allow_literal),
        bot_token=_secret(spec, "bot_token", name=name, allow_literal=allow_literal),
        scopes=frozenset(scopes),
        channels=_ids(spec, "channels", name=name),
        users=_ids(spec, "users", name=name),
        rate_limit=rate_limit,
        user_rate_limit=user_rate_limit,
    )


@lru_cache(maxsize=4)
def _parse(raw: str, allow_literal: bool) -> dict[str, SlackChannel]:
    shape = "{platform, tenant, manifest, team_id, signing_secret, bot_token}"
    return {
        name: _parse_one(name, spec, allow_literal=allow_literal)
        for name, spec in parse_registry(raw, kind="channel", shape=shape).items()
    }


def parse_channels(settings: Any) -> dict[str, SlackChannel]:
    """`FELIX_CHANNELS`, validated; parsed once per distinct value. `ValueError` naming what is wrong."""
    return _parse(str(getattr(settings, "channels", "") or ""), development_install(settings))


async def resolve_secret(settings: Any, value: str) -> str:
    """A `secret:NAME` (or development literal) resolved, and registered for masking -- a literal
    too, which `resolve_secret_value` passes through unregistered."""
    from felix.secrets import build_secrets, register_resolved_secret, resolve_secret_value

    resolved = await resolve_secret_value(build_secrets(settings), value)
    register_resolved_secret(resolved)
    return resolved


async def mask_secrets(settings: Any) -> None:
    """Resolve every channel's secrets once, so they are masked before any run could print one --
    not only after the first reply. Best effort: a secret that does not resolve is the route's to
    report."""
    for channel in parse_channels(settings).values():
        for value in (channel.signing_secret, channel.bot_token):
            try:
                await resolve_secret(settings, value)
            except Exception:
                logger.warning("channel_secret_unresolved channel=%s", channel.id)


__all__ = [
    "MIN_SECRET_BYTES",
    "SLACK_ID_RE",
    "SlackChannel",
    "mask_secrets",
    "parse_channels",
    "resolve_secret",
]
