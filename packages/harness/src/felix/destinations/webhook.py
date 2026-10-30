"""The core `webhook` destination: a post as a signed POST to a `FELIX_WEBHOOK_ENDPOINTS` endpoint.

    FELIX_DESTINATIONS={"ops-brief": {"kind": "webhook", "endpoint": "ops", "tenants": ["acme"]}}

The endpoint is the completion webhooks' own registration -- its URL, signing secret, `tenants`
and `private` flag -- so a receiver verifies a post exactly as it verifies a run's announcement
(Standard Webhooks, `security/standard_webhooks.py`). Both registrations must open to the tenant:
the destination says the tenant may post there, the endpoint that its secret may sign for it.

The `webhook-id` is the edition's `Post.key`, the same on every attempt at that edition, so a
receiver can dedupe too. What an answer or a send error proves is the package's shared
classification (`delivery_for_status`, `delivery_for_send_error`). One attempt, no retry:
the ledger in front of this (`ledger.py`) is what decides whether an edition may be tried again,
and only on an answer that says it did not land.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from felix.destinations import (
    Delivery,
    Destination,
    DestinationError,
    Post,
    delivery_for_send_error,
    delivery_for_status,
)

logger = logging.getLogger("felix.destinations.webhook")

POST_EVENT = "post.created"


def _endpoint(settings: Any, tenant_id: str, dest: Destination) -> Any:
    from felix.durability.webhooks import parse_webhook_endpoints

    name = dest.fields.get("endpoint")
    if not isinstance(name, str) or not name:
        return None
    endpoint = parse_webhook_endpoints(settings).get(name)
    return endpoint if endpoint is not None and endpoint.allows(tenant_id) else None


def _body(tenant_id: str, dest: Destination, post: Post) -> bytes:
    from felix.durability.webhooks import canonical_body

    return canonical_body(
        {
            "type": POST_EVENT,
            "tenant_id": tenant_id,
            "destination": dest.name,
            "edition": post.edition,
            "manifest_id": post.manifest_id,
            "title": post.title,
            "text": post.text,
            # Whether anyone was watching the run that posted; never who it was.
            "unattended": post.unattended,
        }
    )


class WebhookDestination:
    """`kind: webhook`. Fields: `endpoint`, a `FELIX_WEBHOOK_ENDPOINTS` id open to the tenant."""

    def validate(self, settings: Any, tenant_id: str, dest: Destination) -> None:
        endpoint = dest.fields.get("endpoint")
        if not isinstance(endpoint, str) or not endpoint:
            raise DestinationError(f"destination {dest.name} (webhook) needs an `endpoint`")
        if _endpoint(settings, tenant_id, dest) is None:
            # The endpoint id is the operator's and is not repeated: whether it exists for another
            # tenant is not this tenant's to learn.
            raise DestinationError(
                f"destination {dest.name} names a webhook endpoint not registered for this tenant"
            )

    async def secrets(self, settings: Any, tenant_id: str, dest: Destination) -> list[str]:
        """The signing secret, resolved before `post` cleans the text (`DestinationSecrets`), so a
        post that quotes it has it redacted. None for an endpoint not open to the tenant."""
        from felix.secrets import build_secrets, resolve_secret_value

        endpoint = _endpoint(settings, tenant_id, dest)
        if endpoint is None:
            return []
        return [await resolve_secret_value(build_secrets(settings), endpoint.secret)]

    async def deliver(self, settings: Any, tenant_id: str, dest: Destination, post: Post) -> Delivery:
        from felix.durability.webhooks import endpoint_client, sign
        from felix.secrets import build_secrets, register_resolved_secret, resolve_secret_value
        from felix.security.egress import post_for_status

        endpoint = _endpoint(settings, tenant_id, dest)
        if endpoint is None:
            return Delivery("not_posted", detail="the destination's webhook endpoint is no longer registered")
        try:
            secret = await resolve_secret_value(build_secrets(settings), endpoint.secret)
        except Exception:
            logger.warning("destination %s signing secret unresolved", dest.name, exc_info=True)
            secret = ""
        if not secret:
            return Delivery("not_posted", detail="the destination's signing secret could not be resolved")
        register_resolved_secret(secret)
        body = _body(tenant_id, dest, post)
        timestamp = int(time.time())
        headers = {
            "content-type": "application/json",
            "user-agent": "Felix-Webhooks/1",
            "webhook-id": post.key,
            "webhook-timestamp": str(timestamp),
            "webhook-signature": sign(secret, post.key, timestamp, body),
        }
        # Bounded as a whole as well as per read, as a completion webhook is; at most 60 s, inside
        # the harness's `DELIVERY_BUDGET_S`.
        timeout_s = float(settings.webhook_timeout_seconds)
        try:
            client = endpoint_client(settings, endpoint, timeout_s)
        except Exception:
            logger.warning("destination %s client unavailable", dest.name, exc_info=True)
            return Delivery("not_posted", detail="the destination could not be reached")
        try:
            status = await post_for_status(
                client, endpoint.url, content=body, headers=headers, deadline_s=timeout_s
            )
        except Exception as exc:
            delivery = delivery_for_send_error(exc)
            logger.warning(
                "destination %s send failed (%s): %s", dest.name, delivery.status, type(exc).__name__
            )
            return delivery
        return delivery_for_status(status, ref=post.key)


__all__ = ["POST_EVENT", "WebhookDestination"]
