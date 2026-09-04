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


@pytest.mark.asyncio
async def test_bundled_posture_does_not_mount_the_write_routes() -> None:
    """Absent, not refused — which is what the docs claim and what an operator can verify.

    A registered-but-guarded route still appears in `/openapi.json`, still validates request
    bodies before any guard runs, and returns a 405 with no `Allow` header. Not registering
    it at all makes the OpenAPI document honest per deployment and lets Starlette answer
    with a spec-correct `405 Allow: GET`.
    """
    from felix_api.app import create_app

    settings = Settings(
        allow_insecure=True,
        auth_mode="none",
        environment="development",
        object_store="memory",
        database_url="memory://http",
        manifest_source="bundled",
    )
    app = create_app(settings=settings, plugins=[])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        spec = (await c.get("/openapi.json")).json()
        assert "put" not in spec["paths"]["/manifests/{name}"]
        assert "/manifests/{name}/canary" not in spec["paths"]

        resp = await c.put("/manifests/quick", json={"manifest": {}})
        assert resp.status_code == 405
        assert "GET" in resp.headers.get("allow", "")

        # Reads stay open, and report the posture rather than unserved store rows.
        listed = await c.get("/manifests")
        assert listed.status_code == 200
        names = {row["name"] for row in listed.json()["items"]}
        assert {"quick", "governed"} <= names
        assert all(row["version"] is None for row in listed.json()["items"])

        # A version names a stored revision, and there are none.
        assert (await c.get("/manifests/quick?version=1")).status_code == 404


@pytest.mark.asyncio
async def test_store_posture_mounts_them(settings: Settings) -> None:
    """The contrast that gives the assertions above their meaning."""
    from felix_api.app import create_app

    app = create_app(settings=settings, plugins=[])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        spec = (await c.get("/openapi.json")).json()
        assert "put" in spec["paths"]["/manifests/{name}"]
        assert "/manifests/{name}/canary" in spec["paths"]


def test_create_app_boots_without_being_handed_settings() -> None:
    """`create_app()` takes settings as an *optional* parameter, and production omits it.

    `felix_api.main:create_application` calls `create_app()` with no arguments, so the
    resolved config is `cfg = settings or get_settings()` and `settings` itself is None on
    that path. A line reading `settings.` rather than `cfg.` therefore crashed the shipped
    image on boot while the entire suite stayed green, because every test passes settings
    explicitly. This test is the one that does not.
    """
    from felix_api.app import create_app

    app = create_app(plugins=[])
    assert app is not None
    assert any(getattr(r, "path", "") == "/health" for r in app.routes)


@pytest.mark.asyncio
async def test_store_posture_accepts_a_real_write(settings: Settings) -> None:
    """The end-to-end contrast to the 405s, which was previously untestable.

    An earlier version of this test poisoned eleven unrelated tests: the write landed in the
    process-global in-memory store, and a minimal manifest has no `auth.inbound` block, so
    stored `quick` shadowed the bundled file and everything downstream 401'd. It is safe now
    because `tests/conftest.py` resets that store around every test — which is the actual
    fix, rather than avoiding the assertion.
    """
    from felix_api.app import create_app

    app = create_app(settings=settings, plugins=[])
    body = {
        "manifest": {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": "quick"},
            "spec": {"pattern": "react"},
        }
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.put("/manifests/quick", json=body)
        assert resp.status_code in {200, 201}, resp.text
        listed = (await c.get("/manifests")).json()["items"]
        assert any(row["name"] == "quick" for row in listed)
