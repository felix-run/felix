"""A manifest's `secret:NAME` refs resolve only what `FELIX_MANIFEST_SECRETS` opens to its tenant.

Every backend reads any name the process can see, and the manifest picks where the value goes, so
an unchecked ref was a way for anyone with `manifests:write` to have `FELIX_AUTH_API_KEYS` sent to
a server of their choosing. These pin the guard itself, its place in front of the backend on the
compile path, and the boot refusal of a setting that cannot be read.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from felix.manifests.loader import parse_manifest
from felix.manifests.secret_refs import (
    assert_manifest_secrets_permitted,
    manifest_secret_names,
    resolve_outbound_secrets,
)
from felix.secrets import SecretNotPermittedError, check_manifest_secret

from tests.support.factories import make_settings
from tests.support.recording_secrets import BACKEND, install_recording_secrets


def _allow(mapping: dict[str, Any], **overrides: Any) -> Any:
    return make_settings(manifest_secrets=json.dumps(mapping), **overrides)


def _manifest(**spec: Any) -> Any:
    return parse_manifest(
        {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": "scoped"}, "spec": spec}
    )


def _mcp(auth: str, **extra: Any) -> dict[str, Any]:
    return {"name": "docs", "url": "https://mcp.example.com/mcp", "transport": "http", "auth": auth, **extra}


# --- Felix's own secrets ----------------------------------------------------------------------


_INFRA = [
    "DATABASE_URL",
    "redis-url",
    "POSTGRES_PASSWORD",
    "PGPASSWORD",
    "MINIO_ROOT_USER",
    "MINIO_ROOT_PASSWORD",
    "TUNNEL_TOKEN",
    "gh-token",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN",
    "GOOGLE_APPLICATION_CREDENTIALS",
]
_FELIX_OWN = [
    "FELIX_AUTH_API_KEYS",
    "felix_auth_api_keys",
    "felix-auth-api-keys",
    "felix/auth_api_keys",
    "Felix-Jwks-Private",
    "FELIX_DATABASE_URL",
    "felix/github_token_key",
    "ANTHROPIC_API_KEY",
    "github-token-key",
    "GOOGLE_COOKIE_KEY",
]


@pytest.mark.parametrize("name", _FELIX_OWN + _INFRA)
def test_felix_own_secrets_are_refused_for_every_tenant(name: str) -> None:
    # Listing one of these fails `parse_manifest_secrets`, and `check_manifest_secret` reads an
    # unparseable setting as an empty allowlist -- so the call below would refuse it as unlisted even
    # without the reserved-name check. The message match is what pins that the reserved-name check
    # is the one that refused it.
    settings = _allow({name: "*"})
    with pytest.raises(SecretNotPermittedError, match="one of Felix's own credentials") as caught:
        check_manifest_secret(name, "acme", settings)
    assert name in str(caught.value)
    assert "FELIX_MANIFEST_SECRETS" in str(caught.value)


def test_github_token_is_an_ordinary_integration_name() -> None:
    # A tenant's own GitHub integration is the commonest legitimate ref; only `gh`'s GH_TOKEN is the
    # process's.
    check_manifest_secret("GITHUB_TOKEN", "acme", _allow({"GITHUB_TOKEN": ["acme"]}))


@pytest.mark.parametrize(
    "name", ["felix-auth-api-keys", "felix/auth_api_keys", "ANTHROPIC_API_KEY", "PGPASSWORD"]
)
def test_listing_a_felix_secret_fails_the_boot(name: str) -> None:
    with pytest.raises(RuntimeError, match=rf"FELIX_MANIFEST_SECRETS: '{name}' is one of"):
        _allow({name: "*"}).validate_runtime()


# --- secrets an operator registry already binds -----------------------------------------------


@pytest.mark.parametrize(
    ("setting", "value", "owner"),
    [
        (
            "egress_credentials",
            {"gh": {"host": "api.github.com", "secret": "secret:SHARED_TOKEN", "tenants": ["beta"]}},
            "FELIX_EGRESS_CREDENTIALS",
        ),
        (
            "webhook_endpoints",
            {
                "ops": {
                    "url": "https://hooks.example.com/x",
                    "secret": "secret:shared-token",
                    "tenants": ["acme"],
                }
            },
            "FELIX_WEBHOOK_ENDPOINTS",
        ),
        ("secret_names", "OTHER,shared_token", "FELIX_SECRET_NAMES"),
    ],
)
def test_a_secret_an_operator_registry_binds_cannot_be_opened_to_manifests(
    setting: str, value: Any, owner: str
) -> None:
    raw = value if isinstance(value, str) else json.dumps(value)
    # The registry alone is a valid configuration; the overlap is what fails it. Matched after
    # normalisation, so `shared-token` in the registry still owns `SHARED_TOKEN`.
    make_settings(**{setting: raw}).validate_runtime()
    with pytest.raises(RuntimeError, match=rf"'SHARED_TOKEN' is already bound by {owner}"):
        _allow({"SHARED_TOKEN": ["acme"]}, **{setting: raw}).validate_runtime()


@pytest.mark.parametrize(
    ("setting", "value", "owner"),
    [
        (
            "triggers",
            {"t": {"tenant": "acme", "secret": ["secret:OLD_HOOK", "secret:SHARED_TOKEN"]}},
            "FELIX_TRIGGERS",
        ),
        ("channels", {"c": {"bot_token": "secret:SHARED_TOKEN"}}, "FELIX_CHANNELS"),
        (
            "model_provider_options",
            {"groq": {"api_key": {"secret": "SHARED_TOKEN"}}},
            "FELIX_MODEL_PROVIDER_OPTIONS",
        ),
        ("push_vapid_private_key", "secret:SHARED_TOKEN", "FELIX_PUSH_VAPID_PRIVATE_KEY"),
    ],
)
def test_every_operator_registry_is_read_for_bound_secrets(setting: str, value: Any, owner: str) -> None:
    from felix.secrets import assert_manifest_secrets_unbound

    raw = value if isinstance(value, str) else json.dumps(value)
    with pytest.raises(ValueError, match=rf"already bound by {owner}"):
        assert_manifest_secrets_unbound(_allow({"SHARED_TOKEN": "*"}, **{setting: raw}))


# --- the allowlist ----------------------------------------------------------------------------


def test_an_unlisted_secret_is_refused() -> None:
    settings = _allow({"DOCS_TOKEN": "*"})
    with pytest.raises(SecretNotPermittedError, match="not permitted for this tenant") as caught:
        check_manifest_secret("OTHER_TOKEN", "acme", settings)
    assert "OTHER_TOKEN" in str(caught.value)
    assert "FELIX_MANIFEST_SECRETS" in str(caught.value)


def test_nothing_is_allowed_when_the_setting_is_empty() -> None:
    with pytest.raises(SecretNotPermittedError):
        check_manifest_secret("DOCS_TOKEN", "default", make_settings())


def test_a_secret_listed_for_one_tenant_is_refused_for_another() -> None:
    settings = _allow({"ACME_TOKEN": ["acme"]})
    check_manifest_secret("ACME_TOKEN", "acme", settings)
    with pytest.raises(SecretNotPermittedError, match="ACME_TOKEN"):
        check_manifest_secret("ACME_TOKEN", "globex", settings)


def test_a_star_opens_a_secret_to_every_tenant() -> None:
    settings = _allow({"DOCS_TOKEN": "*"})
    for tenant in ("acme", "globex", "default"):
        check_manifest_secret("DOCS_TOKEN", tenant, settings)


def test_a_listed_name_is_matched_exactly_not_by_normalised_spelling() -> None:
    # The env backend would resolve `docs-token` to `DOCS_TOKEN`; allowing `DOCS_TOKEN` must not
    # also allow spellings the operator never wrote.
    with pytest.raises(SecretNotPermittedError):
        check_manifest_secret("docs-token", "acme", _allow({"DOCS_TOKEN": "*"}))


@pytest.mark.parametrize(
    "raw",
    [
        "{not json",
        '["DOCS_TOKEN"]',
        '{"DOCS_TOKEN": []}',
        '{"DOCS_TOKEN": "acme"}',
        '{"DOCS_TOKEN": [""]}',
        '{"DOCS_TOKEN": [1]}',
        '{"": "*"}',
        '{"DOCS TOKEN": "*"}',
    ],
)
def test_a_malformed_setting_fails_validate_runtime(raw: str) -> None:
    with pytest.raises(RuntimeError, match="FELIX_MANIFEST_SECRETS"):
        make_settings(manifest_secrets=raw).validate_runtime()


def test_a_well_formed_setting_passes_validate_runtime() -> None:
    _allow({"DOCS_TOKEN": "*", "ACME_TOKEN": ["acme", "globex"]}).validate_runtime()


# --- what a manifest names --------------------------------------------------------------------


def test_every_outbound_secret_field_is_collected() -> None:
    manifest = _manifest(
        mcp_servers=[_mcp("secret:MCP_AUTH", env={"K": "secret:MCP_ENV"})],
        peers=[{"name": "p", "url": "https://peer.example.com/a2a", "auth": "secret:PEER_AUTH"}],
        containers=[
            {
                "name": "c",
                "gateway_url": "https://gw.example.com",
                "image": "img",
                "auth": "secret:CONTAINER_AUTH",
            }
        ],
        github_publish={"repo": "acme/app", "auth": "secret:PUBLISH_AUTH", "branch_prefix": "felix/"},
    )
    assert manifest_secret_names(manifest) == [
        "MCP_AUTH",
        "MCP_ENV",
        "PEER_AUTH",
        "CONTAINER_AUTH",
        "PUBLISH_AUTH",
    ]


def test_the_write_check_refuses_any_unpermitted_ref() -> None:
    manifest = _manifest(mcp_servers=[_mcp("secret:DOCS_TOKEN", env={"K": "secret:FELIX_JWKS_PRIVATE"})])
    with pytest.raises(SecretNotPermittedError, match="FELIX_JWKS_PRIVATE"):
        assert_manifest_secrets_permitted(manifest, "acme", _allow({"DOCS_TOKEN": "*"}))


# --- the compile path -------------------------------------------------------------------------


async def test_compile_refuses_before_the_backend_is_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = install_recording_secrets(monkeypatch, FELIX_AUTH_API_KEYS="never-read")
    manifest = _manifest(mcp_servers=[_mcp("secret:FELIX_AUTH_API_KEYS")])
    with pytest.raises(SecretNotPermittedError):
        await resolve_outbound_secrets(manifest, make_settings(secrets_backend=BACKEND), tenant_id="acme")
    assert backend.calls == []


async def test_compile_refuses_another_tenants_secret_before_the_backend_is_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = install_recording_secrets(monkeypatch, ACME_TOKEN="acme-value")
    settings = _allow({"ACME_TOKEN": ["acme"]}, secrets_backend=BACKEND)
    manifest = _manifest(
        peers=[{"name": "p", "url": "https://peer.example.com/a2a", "auth": "secret:ACME_TOKEN"}]
    )
    with pytest.raises(SecretNotPermittedError):
        await resolve_outbound_secrets(manifest, settings, tenant_id="globex")
    assert backend.calls == []

    _, peers, _ = await resolve_outbound_secrets(manifest, settings, tenant_id="acme")
    assert backend.calls == ["ACME_TOKEN"]
    assert peers[0].auth == "acme-value"


async def test_github_publish_auth_is_scoped_on_the_compile_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """`github_publish.auth` is resolved by the builder itself, not `resolve_outbound_secrets`. A
    refusal fails the compile as an MCP ref's does, rather than degrading to an agent without the
    tool -- and the backend is never asked."""
    from felix.manifests.builder import build_agent
    from felix.tools.builtins import default_tool_provider

    backend = install_recording_secrets(monkeypatch, PUBLISH_TOKEN="publish-value")
    doc = {
        "apiVersion": "felix/v1",
        "kind": "Agent",
        "metadata": {"name": "publisher"},
        "spec": {
            "pattern": "react",
            "github_publish": {"repo": "acme/app", "auth": "secret:PUBLISH_TOKEN", "branch_prefix": "felix/"},
        },
    }
    # Compiled with no tenant, so as `default`: acme's secret is not its to send.
    refused = _allow({"PUBLISH_TOKEN": ["acme"]}, secrets_backend=BACKEND)
    with pytest.raises(SecretNotPermittedError, match="PUBLISH_TOKEN"):
        await build_agent(doc, default_tool_provider(), settings=refused)
    assert backend.calls == []
    # The control arm: the same compile reaches the backend once the tenant is listed, so the
    # refusal above is the guard's doing and not a block that never ran.
    allowed = _allow({"PUBLISH_TOKEN": ["default"]}, secrets_backend=BACKEND)
    await build_agent(doc, default_tool_provider(), settings=allowed)
    assert backend.calls == ["PUBLISH_TOKEN"]


async def test_a_missing_publish_secret_still_degrades_to_no_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    from felix.manifests.builder import build_agent
    from felix.tools.builtins import default_tool_provider

    install_recording_secrets(monkeypatch)
    doc = {
        "apiVersion": "felix/v1",
        "kind": "Agent",
        "metadata": {"name": "publisher"},
        "spec": {
            "pattern": "react",
            "github_publish": {"repo": "acme/app", "auth": "secret:PUBLISH_TOKEN", "branch_prefix": "felix/"},
        },
    }
    allowed = _allow({"PUBLISH_TOKEN": ["default"]}, secrets_backend=BACKEND)
    agent = await build_agent(doc, default_tool_provider(), settings=allowed)
    assert "publish_commits" not in {t.name for t in agent.tools}


async def test_pattern_builders_receive_the_tenant_scoped_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """`spec.extensions` is tenant input, so a plugin resolving a ref it found there gets the backend
    scoped to the compiling tenant, under `PatternBuildContext["manifest_secrets"]`."""
    from felix.manifests.builder import BuildDeps, build_agent
    from felix.patterns import registry
    from felix.tools.provider import InMemoryToolProvider

    backend = install_recording_secrets(monkeypatch, ACME_TOKEN="acme-value")
    seen: dict[str, Any] = {}

    async def _capture(ctx: dict[str, Any]) -> Any:
        seen.update(ctx)

        class _Agent:
            tools: list[Any] = []

        return _Agent()

    monkeypatch.setitem(registry._patterns, "secret-probe", registry.PatternDescriptor(build=_capture))
    settings = _allow({"ACME_TOKEN": ["acme"]}, secrets_backend=BACKEND)
    doc = _manifest(pattern="secret-probe", extensions={"probe": {"auth": "secret:ACME_TOKEN"}})
    await build_agent(
        doc.model_dump(mode="json", by_alias=True),
        deps=BuildDeps(tools=InMemoryToolProvider(), settings=settings, tenant_id="globex"),
        settings=settings,
    )
    with pytest.raises(SecretNotPermittedError):
        await seen["manifest_secrets"].get("ACME_TOKEN")
    assert backend.calls == []


# --- felix doctor -------------------------------------------------------------------------------


def test_doctor_notes_a_bundled_manifest_whose_secret_is_not_permitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pathlib import Path

    from felix.manifests.loader import clear_bundled_cache
    from felix_cli.main import _manifest_secret_notes

    self_dir = Path(__file__).resolve().parents[2] / "manifests" / "self"
    monkeypatch.setenv("FELIX_MANIFESTS_DIR", str(self_dir))
    clear_bundled_cache()
    try:
        refused = _manifest_secret_notes(_allow({"GITHUB_MCP_TOKEN": ["acme"]}))
        permitted = _manifest_secret_notes(_allow({"GITHUB_MCP_TOKEN": ["default"]}))
    finally:
        clear_bundled_cache()
    assert any("'contributor' names secret:GITHUB_MCP_TOKEN" in n and "tenant default" in n for n in refused)
    assert permitted == []


# --- the self-build stack -------------------------------------------------------------------------


def test_every_secret_the_self_manifests_name_is_open_to_the_self_tenant() -> None:
    """`manifests/self` names `secret:` refs, and `compose.self.yml` is what opens them. A ref added to
    a self manifest without the allowlist entry compiles nowhere on that stack -- every request a 422.
    The tenant is `default`: the one `scripts/dev-key.sh` mints the stack's key for."""
    import re
    from pathlib import Path

    from felix.manifests.loader import load_manifest_file

    from tests.support.compose_yaml import load_compose

    root = Path(__file__).resolve().parents[2]
    raw = load_compose(root / "deploy" / "docker" / "compose.self.yml")["x-self-env"][
        "FELIX_MANIFEST_SECRETS"
    ]
    default = re.fullmatch(r"\$\{FELIX_MANIFEST_SECRETS:-(.*)\}", raw)
    assert default, f"FELIX_MANIFEST_SECRETS in compose.self.yml is not an overridable default: {raw!r}"
    settings = make_settings(manifest_secrets=default.group(1))
    settings.validate_runtime()

    manifests = sorted((root / "manifests" / "self").glob("*.yaml"))
    names = {n for path in manifests for n in manifest_secret_names(load_manifest_file(path))}
    assert names, "the self manifests name no secret: this test is checking nothing"
    for name in sorted(names):
        check_manifest_secret(name, "default", settings)
