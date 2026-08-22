"""HTTP integration — health, metrics, MCP, manifests."""

from __future__ import annotations

import pytest
from felix.config import Settings
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def settings() -> Settings:
    return Settings(
        allow_insecure=True,
        auth_mode="none",
        environment="development",
        object_store="memory",
        database_url="memory://http",
    )


@pytest.mark.asyncio
async def test_health_and_metrics(settings: Settings) -> None:
    from felix_api.app import create_app

    app = create_app(settings=settings, plugins=[])
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        health = await client.get("/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"

        metrics = await client.get("/metrics")
        assert metrics.status_code == 200
        assert "text/plain" in metrics.headers.get("content-type", "")


@pytest.mark.asyncio
async def test_mcp_tools_list_http(settings: Settings) -> None:
    from felix_api.app import create_app

    app = create_app(settings=settings, plugins=[])
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
        assert resp.status_code == 200
        body = resp.json()
        names = {t["name"] for t in body["result"]["tools"]}
        assert "calculator" in names


@pytest.mark.asyncio
async def test_manifests_list_http(settings: Settings) -> None:
    from felix_api.app import create_app

    app = create_app(settings=settings, plugins=[])
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/manifests")
        assert resp.status_code == 200
        assert "items" in resp.json()

        models = await client.get("/v1/models")
        assert models.status_code == 200
        ids = {m["id"] for m in models.json()["data"]}
        assert "quick" in ids


@pytest.mark.asyncio
async def test_a2a_task_get_after_failed_send(settings: Settings) -> None:
    """message/send without model keys fails but still persists a failed task."""
    from felix.a2a import tasks as task_store
    from felix_api.app import create_app

    task_store.clear_tasks()
    app = create_app(settings=settings, plugins=[])
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        send = await client.post(
            "/a2a",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "message/send",
                "params": {
                    "taskId": "t-http-1",
                    "manifest": "quick",
                    "message": {"parts": [{"text": "hi"}]},
                },
            },
        )
        assert send.status_code == 200
        result = send.json()["result"]
        assert result["id"] == "t-http-1"
        assert result["status"]["state"] in {"completed", "failed"}

        got = await client.post(
            "/a2a",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tasks/get",
                "params": {"id": "t-http-1"},
            },
        )
        assert got.status_code == 200
        assert got.json()["result"]["id"] == "t-http-1"


@pytest.mark.asyncio
async def test_jwks_not_configured(settings: Settings) -> None:
    from felix_api.app import create_app

    app = create_app(settings=settings, plugins=[])
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/.well-known/jwks.json")
        assert resp.status_code == 200
        body = resp.json()
        assert body.get("error") == "not_configured"
        assert body.get("keys") == []


@pytest.mark.asyncio
async def test_jwks_from_json(settings: Settings) -> None:
    from felix_api.app import create_app

    settings.jwks_public = '{"keys":[{"kty":"RSA","n":"x","e":"AQAB","kid":"t1"}]}'
    app = create_app(settings=settings, plugins=[])
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/.well-known/jwks.json")
        assert resp.status_code == 200
        assert resp.json()["keys"][0]["kid"] == "t1"


@pytest.mark.asyncio
async def test_plans_crud_http(settings: Settings) -> None:
    from felix_api.app import create_app

    app = create_app(settings=settings, plugins=[])
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        put = await client.put(
            "/plans/p-http",
            json={
                "plan": {
                    "title": "t",
                    "steps": [{"id": "1", "status": "pending"}],
                }
            },
        )
        assert put.status_code == 200
        got = await client.get("/plans/p-http")
        assert got.status_code == 200
        assert got.json()["plan"]["title"] == "t"
        listed = await client.get("/plans")
        assert listed.status_code == 200
        assert any(p["id"] == "p-http" for p in listed.json()["items"])


@pytest.mark.asyncio
async def test_agent_card_http(settings: Settings) -> None:
    from felix_api.app import create_app

    app = create_app(settings=settings, plugins=[])
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/.well-known/agent-card.json")
        assert resp.status_code == 200
        body = resp.json()
        assert "error" not in body or "name" in body
