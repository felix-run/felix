"""Internal queue write-back with content screening + consumer secret gate.

Closes the TypeScript gap where ``POST /internal/sessions/:id/events``
accepted queue-transport results without content screening.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from felix.context import try_get_context
from felix.security.constant_time import constant_time_equal
from pydantic import BaseModel, Field

router = APIRouter(tags=["Internal"])


class SessionEventWrite(BaseModel):
    model_config = {"extra": "forbid"}

    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    content: str | None = None


def _tenant(request: Request) -> str:
    ctx = try_get_context()
    if ctx is not None:
        return ctx.auth.tenant_id
    auth = getattr(request.state, "auth", None)
    return getattr(auth, "tenant_id", "default") if auth else "default"


def _require_consumer_secret(request: Request) -> None:
    """Gate /internal behind FELIX_CONSUMER_SHARED_SECRET when set or non-dev."""
    settings = request.app.state.settings
    expected = getattr(settings, "consumer_shared_secret", "") or ""
    provided = request.headers.get("x-felix-consumer-secret") or ""
    if expected:
        if not constant_time_equal(provided, expected):
            raise HTTPException(status_code=401, detail="invalid_consumer_secret")
        return
    # No secret configured: allow only lean local auth_mode=none.
    if settings.auth_mode != "none" or settings.environment == "production":
        raise HTTPException(
            status_code=503,
            detail="consumer_shared_secret_required",
        )


@router.post("/sessions/{session_id}/events")
async def append_session_event(
    session_id: str, body: SessionEventWrite, request: Request
) -> dict[str, Any]:
    """Append a queue write-back event after content screening."""
    from felix.governance.content_screening import screen_content
    from felix.session.store import append_event

    _require_consumer_secret(request)

    settings = request.app.state.settings
    tenant = _tenant(request)

    # Always screen text content on the landing path (TS hole fix).
    text = body.content
    if text is None and isinstance(body.payload.get("content"), str):
        text = body.payload["content"]
    # Also screen common nested text fields used by queue transports.
    if text is None:
        for key in ("text", "message", "output"):
            val = body.payload.get(key)
            if isinstance(val, str) and val:
                text = val
                break
    if text:
        verdict = await screen_content(text, settings=settings)
        if getattr(verdict, "denied", False) or (
            isinstance(verdict, dict) and verdict.get("denied")
        ):
            raise HTTPException(status_code=422, detail="content_screening_denied")

    event = await append_event(
        settings=settings,
        tenant_id=tenant,
        session_id=session_id,
        event_type=body.type,
        payload={**body.payload, **({"content": text} if text else {})},
    )
    return {"status": "ok", "event": event}
