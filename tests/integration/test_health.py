"""Integration — /health responds when create_app can boot."""

from __future__ import annotations

import pytest
from felix.config import Settings
from felix_api.app import create_app
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_health() -> None:
    # Neither the import nor the call is guarded. `felix-api` is a workspace member, so
    # it is installed wherever this suite runs, and `create_app` has no optional
    # dependency on this path -- a guard around either could only ever convert "the app
    # does not import" or "the app does not start" into a skip, and a skipped health
    # check reads exactly like a passing one.
    settings = Settings(
        allow_insecure=True,
        auth_mode="none",
        environment="development",
    )
    app = create_app(settings=settings, plugins=[])

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
    assert spec["info"]["license"]["name"] == "Apache-2.0"
