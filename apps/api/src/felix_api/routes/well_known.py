"""Well-known discovery — A2A agent-card.json + JWKS."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

router = APIRouter(tags=["System"])


@router.get("/.well-known/agent-card.json")
async def agent_card(request: Request) -> dict[str, Any]:
    from fastapi import HTTPException
    from felix.a2a.card import build_agent_card, is_published
    from felix.manifests.loader import load_bundled

    settings = request.app.state.settings
    try:
        manifest = load_bundled(settings.default_manifest)
    except FileNotFoundError:
        return {"error": "default_manifest_missing", "name": settings.default_manifest}
    # `spec.a2a.publish` was declared and never read, so an agent an operator had
    # explicitly not published was advertised anyway.
    if not is_published(manifest):
        raise HTTPException(status_code=404, detail="not_published")
    base_url = str(request.base_url).rstrip("/")
    return build_agent_card(
        manifest,
        base_url=base_url,
        mcp_enabled=True,
    )


@router.get("/.well-known/jwks.json")
async def jwks(request: Request) -> Any:
    from felix.auth.jwt import public_jwks

    settings = request.app.state.settings
    jwks_public = settings.jwks_public
    if not jwks_public:
        return {"error": "not_configured", "keys": []}
    return public_jwks(jwks_public)
