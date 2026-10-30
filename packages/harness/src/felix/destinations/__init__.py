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
went. An `unknown` is never retried by the harness: a person checks. An HTTP kind does not derive
that mapping itself: `delivery_for_status` and `delivery_for_send_error` are it, shared with the
core `webhook` kind.

A kind receives the destination typed (`Destination`): the harness's own keys (`HARNESS_FIELDS`)
parsed into their fields, and the rest of the registration -- the kind's own fields -- in
`Destination.fields`.

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
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol

from felix.settings_registry import parse_registry, parse_tenants, tenant_allowed

if TYPE_CHECKING:
    from sqlalchemy import ColumnElement

DeliveryStatus = Literal["posted", "not_posted", "unknown"]
LinkPolicy = Literal["strip", "keep"]
#: The most of an adapter's `Delivery.detail` the model is shown.
MAX_DETAIL_CHARS = 300
#: The registration keys the harness reads; everything else is the kind's (`Destination.fields`).
HARNESS_FIELDS = frozenset({"kind", "tenants", "links", "link_hosts"})
#: The longest one delivery may take before the harness stops waiting and records the post as
#: `maybe_posted` (`tool.py`): well inside `ledger.IN_FLIGHT_MS`, so a `posting` row past that is
#: always one whose process stopped, never a delivery still under way.
DELIVERY_BUDGET_S = 90.0


class DestinationError(ValueError):
    """A manifest names a destination it cannot post to: unregistered, not open to its tenant, of a
    kind nobody registered, or registered with fields its kind refuses. Written for the manifest's
    author, and never names anything of a destination the tenant may not see."""


@dataclass(frozen=True, slots=True)
class EditionKey:
    """One manifest's edition at one destination in one tenant: the ledger's key (`ledger.py`) and,
    hashed, the id a provider may dedupe on (`post_key`). An edition is one agent's name for its
    post, so two agents' `brief:2026-10-29` at one destination are two keys."""

    tenant_id: str
    destination: str
    manifest_id: str
    edition: str

    @property
    def post_key(self) -> str:
        return post_key(self)

    def where(self) -> tuple[ColumnElement[bool], ...]:
        """The `destination_posts` row this key names, as SQL conditions."""
        from felix.db.models import DestinationPostRow as Row

        return (
            Row.tenant_id == self.tenant_id,
            Row.destination == self.destination,
            Row.manifest_id == self.manifest_id,
            Row.edition == self.edition,
        )


def post_key(key: EditionKey) -> str:
    """The stable id of one manifest's edition at one destination, hashed over an unambiguous
    encoding so it is safe in a header and no tenant, manifest or edition containing a separator
    lands on another's."""
    encoded = json.dumps([key.tenant_id, key.destination, key.manifest_id, key.edition]).encode()
    return f"post_{hashlib.sha256(encoded).hexdigest()[:40]}"


@dataclass(frozen=True, slots=True)
class Post:
    """What the agent posts, as it leaves: secrets redacted, links as the destination says, and
    screened (`tool.py`)."""

    edition: str
    text: str
    title: str
    manifest_id: str
    #: Stable for one tenant, destination, manifest and edition (`EditionKey.post_key`): an adapter
    #: whose provider dedupes on a caller-chosen id (a webhook's `webhook-id`) sends this.
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


# 4xx answers that do not prove the post was not taken: a 408 can come from a proxy after the
# receiver read the body, and a 409 is what a receiver deduping on a caller-chosen id answers for an
# id it already took -- which is the very case of a post that landed.
_UNCERTAIN_4XX = frozenset({408, 409})


def delivery_for_status(status: int, *, ref: str | None) -> Delivery:
    """What an HTTP answer proves: any 2xx is `posted` (with ``ref``, the provider's id for it); a
    4xx other than 408 and 409 is `not_posted`; anything else -- a 3xx (redirects are not
    followed), a 408, a 409, a 5xx -- is `unknown`."""
    if 200 <= status < 300:
        return Delivery("posted", ref=ref)
    if 400 <= status < 500 and status not in _UNCERTAIN_4XX:
        return Delivery("not_posted", detail=f"the destination refused the post (HTTP {status})")
    return Delivery("unknown", detail=f"the destination answered HTTP {status}, so it may have been posted")


def delivery_for_send_error(exc: BaseException) -> Delivery:
    """What an exception from sending proves: one raised before a byte of the request left -- the
    egress guard's refusal, a connection that was never made -- is `not_posted`; anything else (a
    read timeout, a connection lost or a protocol error once the request may have gone) is
    `unknown`."""
    import httpx

    from felix.security.ssrf import EgressBlocked

    if isinstance(exc, EgressBlocked | httpx.ConnectError | httpx.ConnectTimeout):
        return Delivery("not_posted", detail="the destination could not be reached")
    return Delivery("unknown", detail="the destination did not answer, so it may have been posted")


@dataclass(frozen=True, slots=True)
class Destination:
    """One `FELIX_DESTINATIONS` registration, as a kind receives it."""

    name: str
    kind: str
    # `None` is `"tenants": "*"`.
    tenants: frozenset[str] | None
    links: LinkPolicy = "strip"
    # Under `strip`, the hosts whose links are kept; `None` keeps none.
    link_hosts: tuple[str, ...] | None = None
    #: The registration minus `HARNESS_FIELDS`: the kind's own fields (a webhook's `endpoint`).
    fields: dict[str, Any] = field(default_factory=dict)

    def allows(self, tenant_id: str) -> bool:
        return tenant_allowed(self.tenants, tenant_id)

    def apply_links(self, text: str) -> str:
        """`text` as this destination's link policy leaves it."""
        if self.links == "keep":
            return text
        from felix.governance.outbound_text import strip_links

        if self.link_hosts is None:
            return strip_links(text)
        from felix.security.host_allowlist import HostAllowlist

        allowed = HostAllowlist(self.link_hosts)
        return strip_links(text, keep=lambda host: bool(host) and allowed.allows(host))


class DestinationAdapter(Protocol):
    """One kind of destination, given each destination typed (`Destination`; its own fields in
    `dest.fields`).

    `deliver` has `DELIVERY_BUDGET_S` (90 s) in all, and is cancelled past it -- the post is then
    recorded `maybe_posted`, so a kind bounds its own calls well inside that and answers for
    itself. It must not raise for a delivery that failed: answer `not_posted` only when nothing
    was delivered, else `unknown` (an exception is taken for `unknown`). `detail`, and the message
    of a `DestinationError` from `validate`, reach the model and the manifest's author as written."""

    def validate(self, settings: Any, tenant_id: str, dest: Destination) -> None:
        """Raise `DestinationError` when `dest` cannot be posted to from `tenant_id`. Called when a
        manifest naming it compiles, so a bad registration fails the compile, not the first post."""
        ...

    async def deliver(self, settings: Any, tenant_id: str, dest: Destination, post: Post) -> Delivery:
        """Deliver `post` to `dest`, within `DELIVERY_BUDGET_S`."""
        ...


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


def _harness_spelling(key: str) -> str:
    return key.strip().lower().replace("-", "_")


def parse_destinations(settings: Any) -> dict[str, Destination]:
    """`FELIX_DESTINATIONS`, the harness's part validated: ids, `tenants`, `kind` a non-empty
    string, the link policy -- and no kind field spelled like one of `HARNESS_FIELDS` (`Links`,
    `link-hosts`), which would otherwise be handed to the kind while the operator believed the
    harness read it. `ValueError` naming what is wrong. The kind's own fields are its adapter's to
    check (`resolve_destinations`)."""
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
        fields = {k: v for k, v in spec.items() if k not in HARNESS_FIELDS}
        for key in fields:
            if _harness_spelling(str(key)) in HARNESS_FIELDS:
                raise ValueError(
                    f"destination {name!r} field {key!r} is spelled like the harness's "
                    f"`{_harness_spelling(str(key))}`; write it exactly so"
                )
        out[name] = Destination(
            name=name,
            kind=kind,
            tenants=tenants,
            links=links,
            link_hosts=_link_hosts(name, spec, links),
            fields=fields,
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
        adapter.validate(settings, tenant_id, dest)
        out[name] = dest
    return out


__all__ = [
    "DELIVERY_BUDGET_S",
    "HARNESS_FIELDS",
    "MAX_DETAIL_CHARS",
    "Delivery",
    "DeliveryStatus",
    "Destination",
    "DestinationAdapter",
    "DestinationError",
    "EditionKey",
    "LinkPolicy",
    "Post",
    "delivery_for_send_error",
    "delivery_for_status",
    "destination_kind",
    "list_destination_kinds",
    "parse_destinations",
    "post_key",
    "register_destination_kind",
    "resolve_destinations",
]
