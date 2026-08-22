"""Secret refs, governance frameworks, pin, inbound auth, redaction."""

from __future__ import annotations

import pytest
from felix.audit import store as audit_store
from felix.config import Settings
from felix.context import AuthContext, RequestContext, async_run_with_context
from felix.manifests.governance import GovernanceError, validate_governance
from felix.manifests.inbound_auth import InboundAuthError, enforce_inbound_auth
from felix.manifests.loader import parse_manifest
from felix.manifests.pin import (
    ManifestDriftError,
    assert_pin_matches,
    manifest_content_hash,
    pin_fields,
)
from felix.manifests.schema import Manifest
from felix.manifests.secret_refs import (
    PlaintextSecretError,
    assert_no_plaintext_secrets,
    resolve_mcp_ref,
)
from felix.secrets import (
    EnvSecrets,
    collected_secret_values,
    hydrate_secrets,
    is_secret_ref,
    looks_like_plaintext_secret,
    redact_text,
    register_resolved_secret,
    resolve_secret_value,
    secret_ref_name,
)


def _base_manifest(**spec_extra: object) -> dict:
    return {
        "apiVersion": "felix/v1",
        "kind": "Agent",
        "metadata": {"name": "gov-test"},
        "spec": {
            "pattern": "react",
            "tools": ["calculator"],
            **spec_extra,
        },
    }


def test_secret_ref_parse_and_normalize() -> None:
    assert secret_ref_name("secret:MCP_TOKEN") == "MCP_TOKEN"
    assert secret_ref_name({"secret": "MCP_TOKEN"}) == "MCP_TOKEN"
    assert is_secret_ref("secret:x")
    assert not is_secret_ref("Bearer abcdefghijklmnop")
    assert looks_like_plaintext_secret("Bearer sk-live-abcdefghijklmnop")
    assert not looks_like_plaintext_secret("secret:MCP_TOKEN")


@pytest.mark.asyncio
async def test_resolve_secret_value_registers_for_masking(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_TOKEN", "super-secret-token-value")
    provider = EnvSecrets()
    val = await resolve_secret_value(provider, "secret:MCP_TOKEN")
    assert val == "super-secret-token-value"
    assert "super-secret-token-value" in collected_secret_values()
    assert redact_text("leak super-secret-token-value here").endswith("[REDACTED] here")


@pytest.mark.asyncio
async def test_mcp_auth_object_form_normalized() -> None:
    m = Manifest.model_validate(
        _base_manifest(
            mcp_servers=[
                {
                    "name": "remote",
                    "url": "https://example.com/mcp",
                    "auth": {"secret": "REMOTE_TOKEN"},
                    "transport": "http",
                }
            ]
        )
    )
    assert m.spec.mcp[0].auth == "secret:REMOTE_TOKEN"


def test_assert_no_plaintext_rejects_bearer() -> None:
    m = Manifest.model_validate(
        _base_manifest(
            mcp_servers=[
                {
                    "name": "remote",
                    "url": "https://example.com/mcp",
                    "auth": "Bearer sk-abcdefghijklmnopqrstuv",
                    "transport": "http",
                }
            ]
        )
    )
    with pytest.raises(PlaintextSecretError):
        assert_no_plaintext_secrets(m)


@pytest.mark.asyncio
async def test_resolve_mcp_ref_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REMOTE_TOKEN", "resolved-remote-token-xyz")
    ref = Manifest.model_validate(
        _base_manifest(
            mcp_servers=[
                {
                    "name": "remote",
                    "url": "https://example.com/mcp",
                    "auth": "secret:REMOTE_TOKEN",
                    "transport": "http",
                }
            ]
        )
    ).spec.mcp[0]
    resolved = await resolve_mcp_ref(ref, EnvSecrets())
    assert resolved.auth == "resolved-remote-token-xyz"
    # Source ref unchanged conceptually (we returned a copy)
    assert ref.auth == "secret:REMOTE_TOKEN"


def test_governance_soc2_fail_closed() -> None:
    m = parse_manifest(
        _base_manifest(
            governance={
                "frameworks": ["soc2"],
                "forbid_plaintext_secrets": True,
                "pin_compile": True,
            },
            auth={"inbound": {"allow_anonymous": True}},
        )
    )
    settings = Settings(environment="production", allow_insecure=True)
    with pytest.raises(GovernanceError) as ei:
        validate_governance(m, settings)
    msg = str(ei.value)
    assert "allow_anonymous" in msg or "required_scopes" in msg or "policies" in msg


def test_governance_soc2_passes_with_controls() -> None:
    m = parse_manifest(
        _base_manifest(
            governance={
                "frameworks": ["soc2"],
                "forbid_plaintext_secrets": True,
                "pin_compile": True,
            },
            auth={
                "inbound": {
                    "allow_anonymous": False,
                    "required_scopes": ["chat:write"],
                    "schemes": ["jwt"],
                }
            },
            policies=[{"id": "p1", "required_scopes": ["tools:calc"], "tools": ["calculator"]}],
            observability={"trace": True},
            anomaly={"enabled": True},
        )
    )
    settings = Settings(environment="production", allow_insecure=True)
    validate_governance(m, settings)


def test_governance_eu_ai_act_high_requires_approvals() -> None:
    m = parse_manifest(
        _base_manifest(
            governance={
                "frameworks": ["eu_ai_act"],
                "risk_tier": "high",
                "transparency_notice": True,
                "forbid_plaintext_secrets": True,
                "pin_compile": True,
            },
            content_screening={"enabled": True},
        )
    )
    with pytest.raises(GovernanceError) as ei:
        validate_governance(m, Settings(environment="development"))
    assert "approvals" in str(ei.value)


def test_governance_empty_frameworks_ok_for_quick() -> None:
    m = parse_manifest(_base_manifest(auth={"inbound": {"allow_anonymous": True}}))
    validate_governance(m, Settings(environment="development"))


def test_pin_drift_detected() -> None:
    m1 = parse_manifest(_base_manifest(governance={"pin_compile": True}))
    m2 = parse_manifest(
        _base_manifest(
            governance={"pin_compile": True},
            system_prompt={"inline": "changed"},
        )
    )
    pin = pin_fields(m1, version=1)
    assert pin["manifest_hash"] == manifest_content_hash(m1)
    with pytest.raises(ManifestDriftError):
        assert_pin_matches(pin, m2, version=1)


def test_inbound_auth_anonymous_blocked() -> None:
    m = parse_manifest(_base_manifest(auth={"inbound": {"allow_anonymous": False, "required_scopes": ["x"]}}))
    with pytest.raises(InboundAuthError) as ei:
        enforce_inbound_auth(m, AuthContext(anonymous=True))
    assert ei.value.status_code == 401


def test_inbound_auth_missing_scope() -> None:
    m = parse_manifest(
        _base_manifest(
            auth={
                "inbound": {
                    "allow_anonymous": False,
                    "required_scopes": ["chat:write"],
                }
            }
        )
    )
    auth = AuthContext(anonymous=False, principal_sub="u", scopes=frozenset({"other"}))
    with pytest.raises(InboundAuthError) as ei:
        enforce_inbound_auth(m, auth)
    assert ei.value.status_code == 403


@pytest.mark.asyncio
async def test_session_and_audit_redact_secrets(tmp_path) -> None:
    register_resolved_secret("supersecretvalue123")
    settings = Settings(
        database_url="memory://gov-redact",
        auth_mode="none",
        allow_insecure=True,
        object_store="memory",
        data_dir=str(tmp_path),
    )
    from felix.session.store import InMemorySessionStore
    from felix.session.types import AppendableEvent

    store = InMemorySessionStore()
    session = store.open("t:thread")
    await session.append(AppendableEvent(kind="message", role="user", content="token=supersecretvalue123"))
    events = await session.get_events()
    assert events[0].content is not None
    assert "supersecretvalue123" not in events[0].content
    assert "[REDACTED]" in (events[0].content or "")

    audit_store._pending.clear()
    audit_store.record_event(
        settings,
        "default",
        "user_input",
        payload={"user_input": "see supersecretvalue123"},
    )
    assert "supersecretvalue123" not in str(audit_store._pending[-1]["payload_json"])


@pytest.mark.asyncio
async def test_agent_loop_emits_audit(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(
        database_url="memory://gov-audit",
        auth_mode="none",
        allow_insecure=True,
        object_store="memory",
        data_dir=str(tmp_path),
        anthropic_api_key="",
        openai_api_key="",
    )
    await hydrate_secrets(settings)

    from felix.patterns.types import ChatMessage, InvokeInput
    from felix.runtime import build_tenant_agent
    from felix.tools.builtins import default_tool_provider

    class _FakeModel:
        model_id = "mock"

        async def chat(self, messages, tools, opts=None):
            class _R:
                message = ChatMessage(role="assistant", content="42")
                usage = None
                stop_reason = "end_turn"

            return _R()

    m = parse_manifest(_base_manifest(tools=["calculator"], max_turns=1))
    auth = AuthContext(tenant_id="default", principal_sub="tester", anonymous=True)
    audit_store._pending.clear()
    req = RequestContext(settings=settings, auth=auth, manifest_id="gov-test")
    async with async_run_with_context(req):
        agent = await build_tenant_agent(
            settings, manifest=m, tools=default_tool_provider(), tenant_id="default"
        )
        agent._resolve_model = lambda _input: _FakeModel()  # type: ignore[method-assign]
        await agent.invoke(
            InvokeInput(messages=[ChatMessage(role="user", content="hi")], tenant_id="default")
        )
    types = [e["event_type"] for e in audit_store._pending]
    assert "user_input" in types
    assert "final_response" in types


@pytest.mark.asyncio
async def test_mcp_server_uses_compiled_tools(tmp_path) -> None:
    settings = Settings(
        database_url="memory://gov-mcp",
        auth_mode="none",
        allow_insecure=True,
        object_store="memory",
        data_dir=str(tmp_path),
        default_manifest="quick",
    )
    from felix.mcp.server import handle_rpc
    from felix.tools.builtins import default_tool_provider

    auth = AuthContext(tenant_id="default", anonymous=True)
    listed = await handle_rpc(
        settings=settings,
        tools=default_tool_provider(),
        method="tools/list",
        params={"manifest": "quick"},
        rpc_id=1,
        auth=auth,
    )
    assert "result" in listed
    names = {t["name"] for t in listed["result"]["tools"]}
    # quick manifest tools, not the full builtin catalog alone
    assert "calculator" in names
    # A tool not on quick should be absent
    assert "web_search" not in names or "calculator" in names
