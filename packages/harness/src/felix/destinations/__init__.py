"""Destinations: shared places an operator approved in advance, that an agent may post to.

A scheduled job, a heartbeat or a trigger's run has nobody watching it, and `notify` reaches only
the one person a run acts for. A team's channel, a status page's webhook, a feed -- somewhere a
brief belongs that is not one person's lock screen -- is a *destination*: registered once by the
operator in `FELIX_DESTINATIONS` and named by id in a manifest's `spec.destinations`, which binds
the `post` tool (`destinations/tool.py`). A manifest never carries a URL, a channel or a token:
the registration is the advance approval, so what a tenant author can choose is only which of the
places the operator opened to that tenant.

    FELIX_DESTINATIONS={"ops-brief": {"kind": "webhook", "endpoint": "ops", "tenants": ["acme"]}}

The shape is the operator registries' own (`settings_registry.py`): lowercase ids, an object per
id, and a `tenants` list (or `"*"`, written out). `kind` picks the adapter that delivers a post,
from an open registry (`register_destination_kind`, or `PluginRegistry.register_destination_kind`
for a plugin); core ships `webhook` (`destinations/webhook.py`). The boot checks the shape; the
kind and its own fields are checked when a manifest naming the id compiles, since a plugin's kind
may register after the settings are read.

An adapter answers each delivery with a `Delivery` whose `status` is one of three, and the ledger
(`destinations/ledger.py`) depends on it being honest: `posted` only on the provider's own
confirmation, `not_posted` only when it certainly did not deliver (refused before sending, a 4xx),
and `unknown` for everything in between -- a timeout, a 5xx, a connection lost after the request
went. An `unknown` is never retried by the harness: a person checks.

`Delivery.detail` and `DestinationError`'s message reach the model and the manifest's author
verbatim (the detail cut to `MAX_DETAIL_CHARS`), so a plugin kind writes them for those readers:
never an exception's text, a URL, a token or anything of the operator's.

Links are the operator's call, per destination: `"links": "strip"` (the default) removes every
link as `notify` does; `"keep"` leaves them; `"link_hosts": [...]` with `strip` keeps only links
to those hosts (`allow_hosts` patterns, `*.` allowed). A manifest cannot loosen it. `keep` is a
decision about the destination's own previews as much as its readers: a channel that unfurls a link
fetches it server-side, so a link a model wrote can carry data to any host the moment it is posted.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from felix.settings_registry import parse_registry, parse_tenants, tenant_allowed

DeliveryStatus = Literal["posted", "not_posted", "unknown"]
LinkPolicy = Literal["strip", "keep"]
#: The most of an adapter's `Delivery.detail` the model is shown.
MAX_DETAIL_CHARS = 300


class DestinationError(ValueError):
    """A manifest names a destination it cannot post to: unregistered, not open to its tenant, of a
    kind nobody registered, or registered with fields its kind refuses. Written for the manifest's
    author, and never names anything of a destination the tenant may not see."""


@dataclass(frozen=True, slots=True)
class Post:
    """What the agent posts, as it leaves: secrets redacted and screened (`tool.py`)."""

    edition: str
    text: str
    title: str
    manifest_id: str
    #: Stable for one tenant, destination, manifest and edition (`post_key`): an adapter whose
    #: provider dedupes on a caller-chosen id (a webhook's `webhook-id`) sends this.
    key: str
    #: Whether the run that posted had nobody watching (a job, a heartbeat, a trigger). Never who.
    unattended: bool = True


@dataclass(frozen=True, slots=True)
class Delivery:
    status: DeliveryStatus
    #: The provider's own id for what was posted (a message id), when it gave one.
    ref: str | None = None
    #: Why, for the model and the audit log -- written by the adapter, never an exception's text.
    detail: str = ""


class DestinationAdapter(Protocol):
    """One kind of destination. Both methods take the destination's registered fields (`spec`,
    the `FELIX_DESTINATIONS` object, `kind` and `tenants` included)."""

    def validate(self, settings: Any, tenant_id: str, destination_id: str, spec: dict[str, Any]) -> None:
        """Raise `DestinationError` when `spec` cannot be posted to from `tenant_id`. Called when a
        manifest naming it compiles, so a bad registration fails the compile, not the first post."""
        ...

    async def deliver(
        self, settings: Any, tenant_id: str, destination_id: str, spec: dict[str, Any], post: Post
    ) -> Delivery:
        """Deliver `post`. Never raises for a delivery that failed: answer `not_posted` or
        `unknown` (see the module docstring for which)."""
        ...


@dataclass(frozen=True, slots=True)
class Destination:
    name: str
    kind: str
    # `None` is `"tenants": "*"`.
    tenants: frozenset[str] | None
    spec: dict[str, Any]
    links: LinkPolicy = "strip"
    # Under `strip`, the hosts whose links are kept; `None` keeps none.
    link_hosts: tuple[str, ...] | None = None

    def allows(self, tenant_id: str) -> bool:
        return tenant_allowed(self.tenants, tenant_id)

    def apply_links(self, text: str) -> str:
        """`text` as this destination's link policy leaves it."""
        if self.links == "keep":
            return text
        from felix.push.agent_tool import strip_links

        if self.link_hosts is None:
            return strip_links(text)
        from felix.security.host_allowlist import HostAllowlist

        allowed = HostAllowlist(self.link_hosts)
        return strip_links(text, keep=lambda host: bool(host) and allowed.allows(host))


_kinds: dict[str, DestinationAdapter] = {}


def register_destination_kind(kind: str, adapter: DestinationAdapter) -> None:
    """Make `kind` usable in `FELIX_DESTINATIONS`. A later registration of the same kind replaces
    the earlier, as the other open registries do."""
    if not isinstance(kind, str) or not kind.strip():
        raise ValueError("a destination kind needs a name")
    _kinds[kind] = adapter


def destination_kind(kind: str) -> DestinationAdapter | None:
    _ensure_core_kinds()
    return _kinds.get(kind)


def list_destination_kinds() -> list[str]:
    _ensure_core_kinds()
    return sorted(_kinds)


def _ensure_core_kinds() -> None:
    # Imported here rather than at module scope: `webhook` reaches the fiber store, and this
    # package is imported by the settings validator.
    if "webhook" not in _kinds:
        from felix.destinations.webhook import WebhookDestination

        _kinds.setdefault("webhook", WebhookDestination())


def parse_destinations(settings: Any) -> dict[str, Destination]:
    """`FELIX_DESTINATIONS`, its shape validated: ids, `tenants`, and `kind` a non-empty string.
    `ValueError` naming what is wrong. A kind's own fields are its adapter's to check
    (`resolve_destinations`)."""
    parsed = parse_registry(
        str(getattr(settings, "destinations", "") or ""), kind="destination", shape="{kind, tenants, ...}"
    )
    out: dict[str, Destination] = {}
    for name, spec in parsed.items():
        kind = spec.get("kind")
        if not isinstance(kind, str) or not kind.strip():
            raise ValueError(f"destination {name!r} needs a `kind`")
        tenants = parse_tenants(spec, kind="destination", name=name)
        links = spec.get("links", "strip")
        if links not in ("strip", "keep"):
            raise ValueError(f'destination {name!r} `links` must be "strip" or "keep"')
        out[name] = Destination(
            name=name,
            kind=kind,
            tenants=tenants,
            spec=dict(spec),
            links=links,
            link_hosts=_link_hosts(name, spec, links),
        )
    return out


def _link_hosts(name: str, spec: dict[str, Any], links: str) -> tuple[str, ...] | None:
    if "link_hosts" not in spec:
        return None
    raw = spec["link_hosts"]
    if links != "strip":
        raise ValueError(f'destination {name!r} `link_hosts` only applies with `links: "strip"`')
    if not (isinstance(raw, list) and raw and all(isinstance(h, str) for h in raw)):
        raise ValueError(f"destination {name!r} `link_hosts` must be a non-empty list of host names")
    from felix.security.host_allowlist import normalise_host_pattern

    try:
        return tuple(normalise_host_pattern(h) for h in raw)
    except ValueError:
        raise ValueError(
            f"destination {name!r} `link_hosts` entries must be host names or '*.' and a host name"
        ) from None


def resolve_destinations(settings: Any, tenant_id: str, names: list[str]) -> dict[str, Destination]:
    """The destinations `names` are for `tenant_id`, each checked by its kind's adapter, or
    `DestinationError`. One message for an id that is unregistered and one that is another
    tenant's: which ids exist is not this tenant's to learn."""
    registry = parse_destinations(settings)
    out: dict[str, Destination] = {}
    for name in names:
        dest = registry.get(name)
        if dest is None or not dest.allows(tenant_id):
            raise DestinationError(f"unknown destination: {name}")
        adapter = destination_kind(dest.kind)
        if adapter is None:
            raise DestinationError(f"destination {name} is of kind {dest.kind!r}, which is not installed")
        adapter.validate(settings, tenant_id, name, dest.spec)
        out[name] = dest
    return out


def post_key(tenant_id: str, destination_id: str, manifest_id: str, edition: str) -> str:
    """The stable id of one manifest's edition at one destination, hashed over an unambiguous
    encoding so it is safe in a header and no tenant, manifest or edition containing a separator
    lands on another's."""
    digest = hashlib.sha256(
        json.dumps([tenant_id, destination_id, manifest_id, edition]).encode()
    ).hexdigest()
    return f"post_{digest[:40]}"


__all__ = [
    "MAX_DETAIL_CHARS",
    "Delivery",
    "DeliveryStatus",
    "Destination",
    "DestinationAdapter",
    "DestinationError",
    "LinkPolicy",
    "Post",
    "destination_kind",
    "list_destination_kinds",
    "parse_destinations",
    "post_key",
    "register_destination_kind",
    "resolve_destinations",
]
