"""Management-API RBAC and command_screening defaults."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from felix.config import Settings
from felix.manifests.builder import apply_command_screening
from felix.manifests.schema import CommandScreening
from felix.tools.types import Tool, ToolInput, ToolInvocationCtx, ToolOutput, ToolOutputDict
from httpx import ASGITransport, AsyncClient


@dataclass
class _CmdExec:
    @property
    def transport(self) -> str:
        return "sandbox"

    async def execute(self, args: ToolInput, ctx: ToolInvocationCtx | None = None) -> ToolOutput:
        return f"ran:{args.get('command')}"


def _sandbox_tool() -> Tool:
    return Tool(
        name="shell",
        description="run",
        args_schema={"type": "object", "properties": {"command": {"type": "string"}}},
        executor=_CmdExec(),
    )


@pytest.mark.asyncio
async def test_command_screening_include_defaults_denies_rm() -> None:
    tools = apply_command_screening(
        [_sandbox_tool()],
        CommandScreening(enabled=True, include_defaults=True),
        "t",
    )
    out = await tools[0].executor.execute({"command": "rm -rf /"})
    text = out.content if isinstance(out, ToolOutputDict) else str(out)
    assert "denied" in text.lower()


@pytest.mark.asyncio
async def test_command_screening_include_defaults_off() -> None:
    tools = apply_command_screening(
        [_sandbox_tool()],
        CommandScreening(enabled=True, include_defaults=False, rules=[]),
        "t",
    )
    out = await tools[0].executor.execute({"command": "rm -rf /"})
    text = out.content if isinstance(out, ToolOutputDict) else str(out)
    assert text == "ran:rm -rf /"


@pytest.mark.asyncio
async def test_mgmt_manifests_requires_scope() -> None:
    from felix_api.app import create_app

    keys = (
        '{"sk-ok":{"tenant_id":"default","sub":"ops","scopes":["manifests:write"]},'
        '"sk-bad":{"tenant_id":"default","sub":"ops","scopes":["chat:write"]}}'
    )
    settings = Settings(
        allow_insecure=True,
        auth_mode="api_key",
        auth_api_keys=keys,
        environment="development",
        object_store="memory",
        database_url="memory://rbac",
    )
    app = create_app(settings=settings, plugins=[])
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        denied = await client.get("/manifests", headers={"Authorization": "Bearer sk-bad"})
        assert denied.status_code == 403
        assert "manifests:read" in denied.json()["detail"]

        ok = await client.get("/manifests", headers={"Authorization": "Bearer sk-ok"})
        assert ok.status_code == 200
        assert "items" in ok.json()

        audit_denied = await client.get("/audit", headers={"Authorization": "Bearer sk-ok"})
        assert audit_denied.status_code == 403


@pytest.mark.asyncio
async def test_mgmt_skipped_when_auth_none() -> None:
    from felix_api.app import create_app

    settings = Settings(
        allow_insecure=True,
        auth_mode="none",
        environment="development",
        object_store="memory",
        database_url="memory://rbac-none",
    )
    app = create_app(settings=settings, plugins=[])
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/manifests")
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_governed_chat_with_scopes_no_mcp_secret() -> None:
    """Bundled governed no longer requires FELIX_MCP_AUTH_TOKEN for /chat."""
    from felix_api.app import create_app

    keys = '{"sk":{"tenant_id":"default","sub":"ops","scopes":["chat:write","tools:calc"]}}'
    settings = Settings(
        allow_insecure=True,
        auth_mode="api_key",
        auth_api_keys=keys,
        environment="development",
        object_store="memory",
        database_url="memory://gov",
        anthropic_api_key="",
        openai_api_key="",
    )
    app = create_app(settings=settings, plugins=[])
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/chat",
            headers={"Authorization": "Bearer sk"},
            json={"manifest": "governed", "messages": [{"role": "user", "content": "hi"}]},
        )
        # Auth + compile succeed; model gateway fails closed without provider keys.
        assert resp.status_code == 502
        assert "secret not found" not in resp.text
