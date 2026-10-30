"""The core `webhook` destination: a post as a signed POST to a `FELIX_WEBHOOK_ENDPOINTS` endpoint.

    FELIX_DESTINATIONS={"ops-brief": {"kind": "webhook", "endpoint": "ops", "tenants": ["acme"]}}

The endpoint is the completion webhooks' own registration -- its URL, signing secret, `tenants`
and `private` flag -- so a receiver verifies a post exactly as it verifies a run's announcement
(Standard Webhooks, `security/standard_webhooks.py`). Both registrations must open to the tenant:
the destination says the tenant may post there, the endpoint that its secret may sign for it.

The `webhook-id` is the edition's `Post.key`, the same on every attempt at that edition, so a
receiver can dedupe too. Every 2xx is `posted`; a 4xx other than 408 and 409 is `not_posted`;
anything else, a 3xx included (redirects are not followed), is `unknown`. One attempt, no retry:
the ledger in front of this (`ledger.py`) is what decides whether an edition may be tried again,
and only on an answer that says it did not land.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from felix.destinations import Delivery, DestinationError, Post

logger = logging.getLogger("felix.destinations.webhook")

POST_EVENT = "post.created"
# 4xx answers that do not prove the post was not taken: a 408 can come from a proxy after the
# receiver read the body, and a 409 is what a receiver deduping on `webhook-id` answers for an id it
# already took -- which is the very case of a post that landed.
_UNCERTAIN_4XX = frozenset({408, 409})


def _endpoint(settings: Any, tenant_id: str, spec: dict[str, Any]) -> Any:
    from felix.durability.webhooks import parse_webhook_endpoints

    name = spec.get("endpoint")
    if not isinstance(name, str) or not name:
        return None
    endpoint = parse_webhook_endpoints(settings).get(name)
    return endpoint if endpoint is not None and endpoint.allows(tenant_id) else None


def _body(tenant_id: str, destination_id: str, post: Post) -> bytes:
    from felix.durability.webhooks import canonical_body

    return canonical_body(
        {
            "type": POST_EVENT,
            "tenant_id": tenant_id,
            "destination": destination_id,
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

    def validate(self, settings: Any, tenant_id: str, destination_id: str, spec: dict[str, Any]) -> None:
        if not isinstance(spec.get("endpoint"), str) or not spec.get("endpoint"):
            raise DestinationError(f"destination {destination_id} (webhook) needs an `endpoint`")
        if _endpoint(settings, tenant_id, spec) is None:
            # The endpoint id is the operator's and is not repeated: whether it exists for another
            # tenant is not this tenant's to learn.
            raise DestinationError(
                f"destination {destination_id} names a webhook endpoint not registered for this tenant"
            )

    async def deliver(
        self, settings: Any, tenant_id: str, destination_id: str, spec: dict[str, Any], post: Post
    ) -> Delivery:
        import httpx

        from felix.durability.webhooks import endpoint_client, sign
        from felix.secrets import build_secrets, register_resolved_secret, resolve_secret_value
        from felix.security.egress import post_for_status
        from felix.security.ssrf import EgressBlocked

        endpoint = _endpoint(settings, tenant_id, spec)
        if endpoint is None:
            return Delivery("not_posted", detail="the destination's webhook endpoint is no longer registered")
        try:
            secret = await resolve_secret_value(build_secrets(settings), endpoint.secret)
        except Exception:
            logger.warning("destination %s signing secret unresolved", destination_id, exc_info=True)
            secret = ""
        if not secret:
            return Delivery("not_posted", detail="the destination's signing secret could not be resolved")
        register_resolved_secret(secret)
        body = _body(tenant_id, destination_id, post)
        timestamp = int(time.time())
        headers = {
            "content-type": "application/json",
            "user-agent": "Felix-Webhooks/1",
            "webhook-id": post.key,
            "webhook-timestamp": str(timestamp),
            "webhook-signature": sign(secret, post.key, timestamp, body),
        }
        # Bounded as a whole as well as per read, as a completion webhook is.
        timeout_s = float(settings.webhook_timeout_seconds)
        try:
            client = endpoint_client(settings, endpoint, timeout_s)
        except Exception:
            logger.warning("destination %s client unavailable", destination_id, exc_info=True)
            return Delivery("not_posted", detail="the destination could not be reached")
        try:
            status = await post_for_status(
                client, endpoint.url, content=body, headers=headers, deadline_s=timeout_s
            )
        except (EgressBlocked, httpx.ConnectError, httpx.ConnectTimeout) as exc:
            # Refused or unreachable before a byte of the request left: certainly not delivered.
            logger.warning("destination %s not reached: %s", destination_id, type(exc).__name__)
            return Delivery("not_posted", detail="the destination could not be reached")
        except Exception as exc:
            # A timeout or a connection lost once the request may have gone: it may have landed.
            logger.warning("destination %s delivery uncertain: %s", destination_id, type(exc).__name__)
            return Delivery("unknown", detail="the destination did not answer, so it may have been posted")
        if 200 <= status < 300:
            return Delivery("posted", ref=post.key)
        if status in _UNCERTAIN_4XX:
            return Delivery(
                "unknown", detail=f"the destination answered HTTP {status}, so it may have been posted"
            )
        if 400 <= status < 500:
            return Delivery("not_posted", detail=f"the destination refused the post (HTTP {status})")
        return Delivery(
            "unknown", detail=f"the destination answered HTTP {status}, so it may have been posted"
        )


__all__ = ["POST_EVENT", "WebhookDestination"]
