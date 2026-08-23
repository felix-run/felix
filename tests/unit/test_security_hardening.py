"""Auth fail-closed, MCP deny isError, fs keys, chat HTTP shapes."""

from __future__ import annotations

import pytest
from felix.config import Settings
from felix.tools.types import deny_output
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request


@pytest.fixture
def none_settings() -> Settings:
    return Settings(
        allow_insecure=True,
        auth_mode="none",
        environment="development",
        object_store="memory",
        database_url="memory://sec",
    )


@pytest.mark.asyncio
async def test_api_key_missing_credentials_401() -> None:
    from felix.auth.middleware import authenticate_request

    settings = Settings(
        auth_mode="api_key",
        auth_api_keys='{"sk":{"tenant_id":"t","sub":"u","scopes":[]}}',
        environment="development",
    )
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/chat",
        "raw_path": b"/chat",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 123),
        "server": ("test", 80),
    }
    request = Request(scope)
    result = await authenticate_request(request, settings)
    assert hasattr(result, "status_code")
    assert result.status_code == 401  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_api_key_health_stays_public() -> None:
    from felix.auth.context import ANONYMOUS
    from felix.auth.middleware import authenticate_request

    settings = Settings(auth_mode="api_key", environment="production")
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/health",
        "raw_path": b"/health",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 123),
        "server": ("test", 80),
    }
    request = Request(scope)
    result = await authenticate_request(request, settings)
    assert result is ANONYMOUS or getattr(result, "anonymous", False) is True


@pytest.mark.asyncio
async def test_mcp_deny_sets_is_error(none_settings: Settings, tmp_path) -> None:
    from felix.context import AuthContext
    from felix.manifests.builder import BuildDeps, build_agent
    from felix.manifests.schema import Manifest
    from felix.mcp.server import handle_rpc
    from felix.tools.builtins import default_tool_provider
    from felix.tools.executor import local_executor
    from felix.tools.provider import InMemoryToolProvider
    from felix.tools.types import Tool

    async def _always_deny(_args: dict, _ctx=None):
        return deny_output("[policy denied] blocked", "policy")

    tool = Tool(
        name="blocked",
        description="x",
        args_schema={"type": "object"},
        executor=local_executor(_always_deny),
    )
    provider = InMemoryToolProvider()
    provider.register("blocked", lambda: tool)

    # Compile a one-off agent and exercise MCP against its governed tools via
    # a temporary default by calling the compiled tools path directly.
    none_settings = none_settings.model_copy(
        update={"data_dir": str(tmp_path), "object_store": "memory", "database_url": "memory://mcp-deny"}
    )
    manifest = Manifest.model_validate(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": "deny-mcp"},
            "spec": {
                "pattern": "react",
                "tools": ["blocked"],
                "auth": {"inbound": {"allow_anonymous": True}},
            },
        }
    )
    agent = await build_agent(
        manifest,
        deps=BuildDeps(tools=provider, settings=none_settings, tenant_id="default"),
        settings=none_settings,
    )
    # Simulate MCP tools/call against the compiled tool list. A RequestContext is
    # installed because that is how tools run in production — the limits wrapper now
    # refuses to run an unbudgeted tool rather than silently doing nothing.
    from felix.context import RequestContext, async_run_with_context

    by_name = {t.name: t for t in agent.tools}
    req_ctx = RequestContext(settings=none_settings, auth=AuthContext(), manifest_id="deny-mcp")
    async with async_run_with_context(req_ctx):
        out = await by_name["blocked"].executor.execute({}, None)
    from felix.tools.types import is_wrapper_deny, tool_output_content

    assert is_wrapper_deny(out)
    assert "denied" in tool_output_content(out).lower()

    # Also ensure inbound MCP lists only compiled tools for bundled quick.
    listed = await handle_rpc(
        settings=none_settings,
        tools=default_tool_provider(),
        method="tools/list",
        params={"manifest": "quick"},
        rpc_id=1,
        auth=AuthContext(anonymous=True),
    )
    names = {t["name"] for t in listed["result"]["tools"]}
    assert "calculator" in names


@pytest.mark.asyncio
async def test_fs_object_store_rejects_traversal(tmp_path) -> None:
    from felix.storage.fs import FilesystemObjectStore

    settings = Settings(data_dir=str(tmp_path), object_store="fs")
    store = FilesystemObjectStore(settings)
    await store.put("ok/a.txt", b"hi")
    assert await store.get("ok/a.txt") == b"hi"
    with pytest.raises(ValueError):
        await store.get("../secret")
    with pytest.raises(ValueError):
        await store.put("a/../../etc/passwd", b"x")


@pytest.mark.asyncio
async def test_chat_invalid_thread_id(none_settings: Settings) -> None:
    from felix_api.app import create_app

    app = create_app(settings=none_settings, plugins=[])
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/chat",
            json={
                "manifest": "quick",
                "messages": [{"role": "user", "content": "hi"}],
                "thread_id": "bad:id",
            },
        )
        assert resp.status_code == 400
        assert resp.json()["detail"] == "invalid_thread_id"


@pytest.mark.asyncio
async def test_openai_completions_gateway_error_shape(none_settings: Settings) -> None:
    """Missing model credentials should yield a structured 502, not an ASGI crash."""
    from felix_api.app import create_app

    none_settings.anthropic_api_key = ""
    none_settings.openai_api_key = ""
    app = create_app(settings=none_settings, plugins=[])
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "quick",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert resp.status_code == 502
        body = resp.json()
        assert "error" in body
        assert body["error"]["type"] == "model_gateway_error"


@pytest.mark.asyncio
async def test_internal_requires_secret_when_configured(none_settings: Settings) -> None:
    from felix_api.app import create_app

    none_settings.consumer_shared_secret = "s3cret-value-here"
    app = create_app(settings=none_settings, plugins=[])
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        denied = await client.post(
            "/internal/sessions/s1/events",
            json={"type": "message", "content": "hello"},
        )
        assert denied.status_code == 401
        ok = await client.post(
            "/internal/sessions/s1/events",
            headers={"x-felix-consumer-secret": "s3cret-value-here"},
            json={"type": "message", "content": "hello"},
        )
        assert ok.status_code == 200
        assert ok.json()["status"] == "ok"
