"""The MCP server compiles as the caller `AuthMiddleware` identified, across the SDK's task boundary.

The caller reaches Felix's handlers through a context var the SDK's tasks inherit. Every other MCP
test calls as the anonymous `default` caller, which is also what a lost or replaced caller would
look like, so these call as someone else: a signed-in principal on another tenant, whose manifest
exists only there and admits no anonymous caller.
"""

from __future__ import annotations

from felix.context import AuthContext
from felix.manifests.loader import parse_manifest
from felix.manifests.store import put_version
from felix_api.composition import compose

from tests.support.factories import make_settings
from tests.support.mcp_server import mcp_rpc

TENANT = "acme"
MANIFEST = "acme-closed"
MEMBER = AuthContext(tenant_id=TENANT, principal_sub="u-1", anonymous=False, scheme="apikey")


async def _store() -> object:
    settings = make_settings()
    spec = {"pattern": "react", "auth": {"inbound": {"allow_anonymous": False}}, "tools": ["calculator"]}
    manifest = parse_manifest(
        {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": MANIFEST}, "spec": spec}
    )
    await put_version(settings, TENANT, MANIFEST, manifest)
    return settings


async def test_a_signed_in_caller_on_another_tenant_reaches_that_tenants_manifest() -> None:
    settings = await _store()
    common = {"settings": settings, "tools": compose(settings), "manifest": MANIFEST}
    listed = await mcp_rpc(**common, method="tools/list", auth=MEMBER)
    called = await mcp_rpc(
        **common,
        method="tools/call",
        params={"name": "calculator", "arguments": {"expression": "2*3"}},
        auth=MEMBER,
    )
    assert "calculator" in {t["name"] for t in listed["result"]["tools"]}
    assert called["result"]["content"][0]["text"] == "6"


async def test_the_same_manifest_is_not_there_for_the_default_tenant() -> None:
    settings = await _store()
    out = await mcp_rpc(settings=settings, tools=compose(settings), manifest=MANIFEST, method="tools/list")
    assert out["error"]["code"] == -32004
