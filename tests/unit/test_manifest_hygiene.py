"""A manifest cannot store a credential, and a read cannot return one.

`assert_no_plaintext_secrets` ran at compile and only under `forbid_plaintext_secrets` or
in production, so a manifest carrying `auth: Bearer sk-...` stored fine and
`GET /manifests/{name}` handed the token to `manifests:read`. A sandbox image outside the
allowlist stored fine too and raised inside every build. The route and the CLI now run
one write-time validator, and every read redacts.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.config import Settings
from felix.manifests.governance import GovernanceError, validate_for_write
from felix.manifests.loader import list_bundled, load_bundled, parse_manifest
from felix.manifests.secret_refs import REDACTED, redact_manifest_secrets
from felix.secrets import looks_like_plaintext_secret
from felix_api.app import create_app
from httpx import ASGITransport, AsyncClient

TOKEN = "Bearer sk-live-abcdefghijklmnopqrstuvwxyz0123"
# Assembled so the secret scanner in CI does not mistake a fixture for a leaked token.
JWT = ".".join(("eyJhbGciOiJIUzI1NiJ9", "eyJzdWIiOiJhbGljZSJ9", "c2lnbmF0dXJlLXNpZ25hdHVyZQ"))
KEYS = (
    '{"sk-w": {"tenant_id": "acme", "sub": "w", "scopes": ["manifests:write"]},'
    ' "sk-r": {"tenant_id": "acme", "sub": "r", "scopes": ["manifests:read"]}}'
)


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "database_url": "memory://manifest-hygiene",
        "object_store": "memory",
        "redis_url": "",
        "allow_insecure": True,
        "auth_mode": "api_key",
        "auth_api_keys": KEYS,
        "host": "127.0.0.1",
        "environment": "development",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _doc(name: str = "probe", **spec: Any) -> dict[str, Any]:
    return {
        "apiVersion": "felix/v1",
        "kind": "Agent",
        "metadata": {"name": name},
        "spec": {"pattern": "react", **spec},
    }


def _mcp(name: str = "probe", **fields: Any) -> dict[str, Any]:
    return _doc(name, mcp=[{"name": "svc", "url": "https://mcp.example", **fields}])


async def _put(client: AsyncClient, doc: dict[str, Any]) -> Any:
    return await client.put(
        f"/manifests/{doc['metadata']['name']}",
        json={"manifest": doc},
        headers={"Authorization": "Bearer sk-w"},
    )


@pytest.fixture
def app() -> Any:
    from felix.manifests import store as manifest_store

    manifest_store.reset_memory_store()
    return create_app(settings=_settings(), plugins=[])


# --- the heuristic ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        TOKEN,
        "Basic dXNlcjpwYXNz",
        "token abcdef",
        JWT,
        "sk-live-abc123def456",
        "AKIAIOSFODNN7EXAMPLE",
        "user:password1",
    ],
)
def test_credential_shapes_look_like_secrets(value: str) -> None:
    assert looks_like_plaintext_secret(value)


@pytest.mark.parametrize(
    "value",
    [
        "secret:MCP_TOKEN",
        "",
        "debug",
        "us-east-1",
        "https://mcp.example/path",
        "HOST:localhost",
        "format:pretty",
        "retry:3",
    ],
)
def test_ordinary_values_do_not(value: str) -> None:
    assert not looks_like_plaintext_secret(value)


# --- write time -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_credential_is_refused_at_write_and_the_detail_names_the_field(app: Any) -> None:
    cases = [
        (_mcp(auth=TOKEN), "mcp_servers.svc.auth"),
        (_mcp(auth="user:password1"), "mcp_servers.svc.auth"),
        (
            _doc(peers=[{"name": "peer", "url": "https://peer.example", "auth": "Basic dXNlcjpwYXNz"}]),
            "peers.peer.auth",
        ),
        (
            _doc(
                containers=[{"name": "box", "image": "img", "gateway_url": "https://gw.example", "auth": JWT}]
            ),
            "containers.box.auth",
        ),
        (_mcp(env={"API_KEY": "sk-live-aaaaaaaaaaaa"}), "mcp_servers.svc.env.API_KEY"),
        (_mcp(url="https://u:sk-live-tok@mcp.example"), "mcp_servers.svc.url"),
    ]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for doc, field in cases:
            resp = await _put(client, doc)
            assert resp.status_code == 400, resp.text
            assert field in resp.json()["detail"], resp.text
            for value in ("sk-live", JWT, "dXNlcjpwYXNz", "password1"):
                assert value not in resp.text, "the refusal never echoes the value"
        assert (await _put(client, _mcp(auth="secret:MCP_TOKEN"))).status_code == 200
        # Development: an env value that is not credential-shaped is an ordinary setting.
        assert (await _put(client, _mcp(env={"LOG_LEVEL": "debug"}))).status_code == 200


def test_the_strict_env_rule_applies_under_frameworks_or_production() -> None:
    plain_env = parse_manifest(_mcp(env={"LOG_LEVEL": "debug"}))
    validate_for_write(plain_env, _settings())
    with pytest.raises(GovernanceError, match="LOG_LEVEL"):
        validate_for_write(plain_env, _settings(environment="production"))
    strict = parse_manifest(
        _doc(
            mcp=[{"name": "svc", "url": "https://mcp.example", "env": {"LOG_LEVEL": "debug"}}],
            governance={"forbid_plaintext_secrets": True},
        )
    )
    with pytest.raises(GovernanceError, match="LOG_LEVEL"):
        validate_for_write(strict, _settings())


@pytest.mark.asyncio
async def test_a_disallowed_sandbox_image_is_refused_at_write(app: Any) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        bad = await _put(client, _doc(sandboxes=[{"name": "box", "binding": "evil/image:latest"}]))
        assert bad.status_code == 400
        assert "FELIX_SANDBOX_ALLOWED_IMAGES" in bad.json()["detail"]
        ok = await _put(client, _doc(sandboxes=[{"name": "box", "binding": "python:3.14-slim"}]))
        assert ok.status_code == 200, ok.text


@pytest.mark.asyncio
async def test_an_unknown_checkpointer_is_refused_at_write_by_the_same_validator(app: Any) -> None:
    """The last write-time rule that lived in the route and the CLI separately."""
    with pytest.raises(GovernanceError, match="checkpointer"):
        validate_for_write(parse_manifest(_doc(memory={"checkpointer": "no-such-backend"})), _settings())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await _put(client, _doc(memory={"checkpointer": "no-such-backend"}))
        assert resp.status_code == 400
        assert "checkpointer" in resp.json()["detail"]


def test_the_cli_refuses_what_the_route_refuses(tmp_path: Any) -> None:
    """`felix validate-manifest` said `ok` to a manifest `PUT /manifests` refused."""
    import yaml
    from felix_cli.main import app as cli
    from typer.testing import CliRunner

    path = tmp_path / "leaky.yaml"
    path.write_text(yaml.safe_dump(_mcp("leaky", auth=TOKEN)))
    result = CliRunner().invoke(cli, ["validate-manifest", str(path), "--no-resolve-egress"])
    assert result.exit_code == 1
    assert "mcp_servers.svc.auth" in result.output
    clean = tmp_path / "clean.yaml"
    clean.write_text(yaml.safe_dump(_mcp("clean", auth="secret:MCP_TOKEN")))
    assert CliRunner().invoke(cli, ["validate-manifest", str(clean), "--no-resolve-egress"]).exit_code == 0


# --- read time --------------------------------------------------------------------------


def test_redaction_keeps_refs_and_replaces_every_literal() -> None:
    doc = _doc(
        mcp=[
            {
                "name": "a",
                "url": "https://u:pw@mcp.example:8443/x?y=1",
                "auth": JWT,
                "env": {"K": "debug", "R": "secret:REF", "E": ""},
            }
        ],
        peers=[{"name": "p", "url": "https://peer.example", "auth": "secret:PEER"}],
        containers=[
            {"name": "c", "image": "i", "auth": "not-heuristic-shaped", "gateway_url": "http://u:p@gw"}
        ],
    )
    out = redact_manifest_secrets(doc)
    mcp = out["spec"]["mcp"][0]
    assert mcp["auth"] == REDACTED
    assert mcp["env"] == {"K": REDACTED, "R": "secret:REF", "E": ""}
    assert mcp["url"] == "https://mcp.example:8443/x?y=1", "userinfo stripped, the rest kept"
    assert out["spec"]["peers"][0]["auth"] == "secret:PEER"
    assert out["spec"]["containers"][0]["auth"] == REDACTED, "any literal auth, not only heuristic matches"
    assert out["spec"]["containers"][0]["gateway_url"] == "http://gw"
    assert doc["spec"]["mcp"][0]["auth"] == JWT, "the input is not mutated"
    assert redact_manifest_secrets({"spec": "not a dict"}) == {"spec": "not a dict"}


@pytest.mark.asyncio
async def test_a_stored_credential_never_reaches_a_reader_or_the_writer(app: Any) -> None:
    """Written through the store, as a manifest from before the write-time check was."""
    from felix.manifests import store as manifest_store

    settings = app.state.settings
    leaky = parse_manifest(_mcp("leaky", auth=JWT))
    await manifest_store.put_version(settings, "acme", "leaky", leaky)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resolved = await client.get("/manifests/leaky", headers={"Authorization": "Bearer sk-r"})
        assert resolved.status_code == 200
        assert JWT not in resolved.text
        assert resolved.json()["manifest"]["spec"]["mcp"][0]["auth"] == REDACTED
        versioned = await client.get(
            "/manifests/leaky", params={"version": 1}, headers={"Authorization": "Bearer sk-r"}
        )
        assert versioned.status_code == 200
        assert versioned.json()["version"] == 1
        assert versioned.json()["manifest"]["spec"]["mcp"][0]["auth"] == REDACTED
        assert JWT not in versioned.text
        written = await _put(client, _mcp("fresh", auth="secret:MCP_TOKEN", env={"R": "secret:X"}))
        assert written.status_code == 200
        assert written.json()["manifest"]["spec"]["mcp"][0]["auth"] == "secret:MCP_TOKEN", (
            "refs are not redacted"
        )


# --- the bundled manifests that bind a shell ------------------------------------------------


def test_no_bundled_manifest_binding_a_shell_allows_anonymous_callers() -> None:
    """The rule, not the instance: the next bundled manifest with a local shell inherits it."""
    shells = {
        name
        for name in list_bundled()
        if any(t.name == "local_shell" for t in load_bundled(name).spec.client_tools)
    }
    assert "cowork" in shells, "the case that motivated the rule"
    for name in shells:
        assert load_bundled(name).spec.auth.inbound.allow_anonymous is False, name
