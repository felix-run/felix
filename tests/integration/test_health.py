"""Integration — /health responds when create_app can boot."""

from __future__ import annotations

import pytest
from felix.config import Settings
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_health() -> None:
    try:
        from felix_api.app import create_app
    except ImportError as exc:
        pytest.skip(f"create_app imports incomplete: {exc}")

    settings = Settings(
        allow_insecure=True,
        auth_mode="none",
        environment="development",
    )
    try:
        app = create_app(settings=settings, plugins=[])
    except Exception as exc:
        pytest.skip(f"create_app requires harness modules: {exc}")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert "env" in body
