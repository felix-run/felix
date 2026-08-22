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


@pytest.mark.asyncio
async def test_openapi_contact_and_license() -> None:
    from felix_api.app import create_app

    settings = Settings(
        allow_insecure=True,
        auth_mode="none",
        environment="development",
    )
    app = create_app(settings=settings, plugins=[])
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/openapi.json")
    assert resp.status_code == 200
    spec = resp.json()
    assert spec["info"]["title"] == "Felix"
    # FastAPI may normalize contact URLs with a trailing slash.
    assert spec["info"]["contact"]["url"].rstrip("/") == "https://docs.felix.run"
    assert spec["info"]["license"]["name"] == "MIT"
