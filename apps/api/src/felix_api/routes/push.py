"""Web Push subscriptions: which browsers to wake when a run is waiting on a person.

All three routes need `approvals:read`, the scope the `approval_required` frame itself is
gated on: a subscription is a standing request to be told about approvals, and a push must not
tell anyone what that frame would not. Reading the key and subscribing answer 503 until
the operator sets a VAPID key (`FELIX_PUSH_VAPID_PRIVATE_KEY` + `FELIX_PUSH_VAPID_SUBJECT`), so
a client can tell "this deployment does not push" from a failure.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from felix.auth.mgmt import (
    SCOPE_APPROVALS_READ,
    require_mgmt_scopes,
    subject_from_request,
    tenant_id_from_request,
)
from pydantic import BaseModel, Field

router = APIRouter(tags=["Push"])

# A push service's endpoint is a long capability URL; the browser's keys are short base64url.
MAX_ENDPOINT_CHARS = 2048
MAX_KEY_CHARS = 256


class SubscriptionKeys(BaseModel):
    model_config = {"extra": "forbid"}

    p256dh: str = Field(min_length=1, max_length=MAX_KEY_CHARS)
    auth: str = Field(min_length=1, max_length=MAX_KEY_CHARS)


class SubscribeRequest(BaseModel):
    """`PushSubscription.toJSON()`, as the browser produces it."""

    # `expirationTime` is part of what the browser hands over and means nothing to a sender.
    model_config = {"extra": "ignore"}

    endpoint: str = Field(min_length=1, max_length=MAX_ENDPOINT_CHARS)
    keys: SubscriptionKeys


class UnsubscribeRequest(BaseModel):
    model_config = {"extra": "forbid"}

    endpoint: str = Field(min_length=1, max_length=MAX_ENDPOINT_CHARS)


def _require_configured(request: Request) -> None:
    from felix.push.notify import enabled

    if not enabled(request.app.state.settings):
        raise HTTPException(status_code=503, detail="push_not_configured")


@router.get("/vapid-public-key")
async def vapid_public_key(request: Request) -> dict[str, Any]:
    """The application server key a browser passes to `pushManager.subscribe`."""
    from felix.push.notify import vapid_private_key
    from felix.push.webpush import public_key_b64

    require_mgmt_scopes(request, SCOPE_APPROVALS_READ)
    _require_configured(request)
    key = await vapid_private_key(request.app.state.settings)
    return {"public_key": public_key_b64(key)}


@router.post("/subscriptions")
async def subscribe(body: SubscribeRequest, request: Request) -> dict[str, Any]:
    """Register (or refresh) this browser. Idempotent on the endpoint."""
    from felix.push import store
    from felix.push.notify import host_allowed
    from felix.push.webpush import validate_subscription_keys

    require_mgmt_scopes(request, SCOPE_APPROVALS_READ)
    _require_configured(request)
    settings = request.app.state.settings
    if not host_allowed(settings, body.endpoint):
        raise HTTPException(status_code=422, detail="push_endpoint_not_allowed")
    try:
        validate_subscription_keys(body.keys.p256dh, body.keys.auth)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="push_keys_malformed") from exc
    try:
        row = await store.upsert(
            settings,
            tenant_id_from_request(request),
            endpoint=body.endpoint,
            p256dh=body.keys.p256dh,
            auth=body.keys.auth,
            principal_subj=subject_from_request(request),
        )
    except store.TooManySubscriptions as exc:
        raise HTTPException(status_code=409, detail="push_subscription_limit") from exc
    # The endpoint is a capability (anyone holding it can push to that browser): never echoed.
    return {"id": row["id"], "created_at": row["created_at"]}


@router.delete("/subscriptions")
async def unsubscribe(body: UnsubscribeRequest, request: Request) -> dict[str, Any]:
    """Forget this browser. Not an error when it was never subscribed.

    Answered whether or not push is configured: a deployment that turned push off must still
    let a browser take its row back.
    """
    from felix.push import store

    require_mgmt_scopes(request, SCOPE_APPROVALS_READ)
    removed = await store.remove(request.app.state.settings, tenant_id_from_request(request), body.endpoint)
    return {"removed": removed}
