"""Over the wire: a manifest naming a secret its tenant may not reference is refused, and stored never.

Two tenants on api_key auth, so "this tenant" is a key's tenant rather than the `default` every
anonymous request shares — a route that checked the wrong tenant would pass a single-tenant test.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from felix.manifests.loader import parse_manifest

from tests.support.recording_secrets import BACKEND, install_recording_secrets

ACME = "acme-writer-not-a-secret"
GLOBEX = "globex-writer-not-a-secret"


def _keys(**extra: str) -> dict[str, str]:
    scopes = ["manifests:write", "manifests:read", "chat:write"]
    return {
        "FELIX_AUTH_MODE": "api_key",
        "FELIX_AUTH_API_KEYS": json.dumps(
            {
                ACME: {"tenant_id": "acme", "sub": "acme-writer", "scopes": scopes},
                GLOBEX: {"tenant_id": "globex", "sub": "globex-writer", "scopes": scopes},
            }
        ),
        **extra,
    }


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _doc(name: str, auth: str) -> dict[str, Any]:
    return {
        "apiVersion": "felix/v1",
        "kind": "Agent",
        "metadata": {"name": name},
        "spec": {
            "auth": {"inbound": {"allow_anonymous": True}},
            "mcp_servers": [
                {"name": "docs", "url": "https://mcp.example.com/mcp", "transport": "http", "auth": auth}
            ],
        },
    }


async def _versions(app: Any, name: str, key: str) -> list[Any]:
    resp = await app.client.get(f"/manifests/{name}/versions", headers=_bearer(key))
    assert resp.status_code == 200, resp.text
    return resp.json()["items"]


async def test_a_put_naming_felix_own_secret_is_422_and_stores_nothing(boot: Any) -> None:
    async with boot([], env=_keys()) as app:
        resp = await app.client.put(
            "/manifests/e2e-exfil",
            json={"manifest": _doc("e2e-exfil", "secret:FELIX_AUTH_API_KEYS")},
            headers=_bearer(ACME),
        )
        assert resp.status_code == 422, resp.text
        detail = resp.json()["detail"]
        assert "FELIX_AUTH_API_KEYS" in detail
        assert "FELIX_MANIFEST_SECRETS" in detail
        # Never the value: the key the request itself authenticated with is in that variable.
        assert ACME not in resp.text
        assert await _versions(app, "e2e-exfil", ACME) == []


async def test_a_secret_listed_for_one_tenant_is_refused_to_another_at_write(boot: Any) -> None:
    env = _keys(FELIX_MANIFEST_SECRETS=json.dumps({"ACME_DOCS_TOKEN": ["acme"]}))
    async with boot([], env=env) as app:
        body = {"manifest": _doc("e2e-docs", "secret:ACME_DOCS_TOKEN")}
        refused = await app.client.put("/manifests/e2e-docs", json=body, headers=_bearer(GLOBEX))
        assert refused.status_code == 422, refused.text
        assert "ACME_DOCS_TOKEN" in refused.json()["detail"]
        assert await _versions(app, "e2e-docs", GLOBEX) == []

        stored = await app.client.put("/manifests/e2e-docs", json=body, headers=_bearer(ACME))
        assert stored.status_code == 200, stored.text
        assert [v["version"] for v in await _versions(app, "e2e-docs", ACME)] == [1]


async def test_a_stored_manifest_naming_another_tenants_secret_fails_its_compile(
    boot: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row written before the write check existed: the compile refuses it, and the backend is
    never asked for the value. The boot fixture stores under `default`; acme alone may use it."""
    backend = install_recording_secrets(monkeypatch, ACME_DOCS_TOKEN="acme-docs-value")
    env = {
        "FELIX_SECRETS_BACKEND": BACKEND,
        "FELIX_MANIFEST_SECRETS": json.dumps({"ACME_DOCS_TOKEN": ["acme"]}),
    }
    manifest = parse_manifest(_doc("e2e-stored", "secret:ACME_DOCS_TOKEN"))
    async with boot([], env=env, manifests={"e2e-stored": manifest}) as app:
        resp = await app.client.post(
            "/chat", json={"manifest": "e2e-stored", "messages": [{"role": "user", "content": "hi"}]}
        )
        assert app.spy.prompts == []
    assert resp.status_code == 422, resp.text
    assert "ACME_DOCS_TOKEN" in resp.json()["detail"]
    assert "acme-docs-value" not in resp.text
    assert backend.calls == []


async def test_a_stored_version_naming_an_unlisted_secret_cannot_be_made_live(boot: Any) -> None:
    """Canary and rollback activate a version stored earlier, perhaps before the write check: they
    refuse it on the same terms as a PUT, and leave the pointer where it was."""
    manifest = parse_manifest(_doc("e2e-old-row", "secret:UNLISTED_TOKEN"))
    async with boot([], manifests={"e2e-old-row": manifest}) as app:
        canary = await app.client.post(
            "/manifests/e2e-old-row/canary", json={"canary_version": 1, "canary_weight": 10}
        )
        rollback = await app.client.post("/manifests/e2e-old-row/rollback", json={"version": 1})
        versions = (await app.client.get("/manifests/e2e-old-row/versions")).json()["items"]
    for resp in (canary, rollback):
        assert resp.status_code == 422, resp.text
        assert "UNLISTED_TOKEN" in resp.json()["detail"]
    assert [v["canary"] for v in versions] == [False]


def _publisher(name: str, auth: str) -> dict[str, Any]:
    """A manifest whose only secret ref is `github_publish.auth`: resolved at compile, sent nowhere
    until the tool is called, so a compile reaches the backend and nothing past it."""
    return {
        "apiVersion": "felix/v1",
        "kind": "Agent",
        "metadata": {"name": name},
        "spec": {
            "pattern": "react",
            "github_publish": {"repo": "acme/app", "auth": auth, "branch_prefix": "felix/"},
        },
    }


async def test_the_compile_checks_the_callers_tenant_not_default(
    boot: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same manifest stored for two tenants, its secret open to one. A builder that checked
    `default`, or any tenant but the caller's, fails one half of this."""
    from felix.manifests.store import put_version
    from felix_ai.providers.scripted import ScriptedTurn

    backend = install_recording_secrets(monkeypatch, ACME_DOCS_TOKEN="acme-docs-value")
    env = _keys(
        FELIX_SECRETS_BACKEND=BACKEND,
        FELIX_MANIFEST_SECRETS=json.dumps({"ACME_DOCS_TOKEN": ["acme"]}),
    )
    manifest = parse_manifest(_publisher("e2e-publisher", "secret:ACME_DOCS_TOKEN"))
    body = {"manifest": "e2e-publisher", "messages": [{"role": "user", "content": "hi"}]}
    async with boot([ScriptedTurn(content="published nothing")], env=env) as app:
        for tenant in ("acme", "globex"):
            await put_version(app.settings, tenant, "e2e-publisher", manifest)

        acme = await app.client.post("/chat", json=body, headers=_bearer(ACME))
        assert acme.status_code == 200, acme.text
        assert backend.calls == ["ACME_DOCS_TOKEN"]

        globex = await app.client.post("/chat", json=body, headers=_bearer(GLOBEX))
        assert globex.status_code == 422, globex.text
        assert "ACME_DOCS_TOKEN" in globex.json()["detail"]
        assert backend.calls == ["ACME_DOCS_TOKEN"]
        assert len(app.spy.prompts) == 1


async def test_rolling_back_to_a_version_with_an_unlisted_secret_keeps_the_live_one(boot: Any) -> None:
    """v1 predates the write check and names a secret acme may not use; v2 is clean and made live. The
    rollback is refused and v2 stays active -- under a non-default tenant, so the check reads the
    caller's tenant and the caller's rows."""
    from felix.manifests.store import put_version

    async with boot([], env=_keys()) as app:
        await put_version(
            app.settings, "acme", "e2e-rollback", parse_manifest(_doc("e2e-rollback", "secret:UNLISTED"))
        )
        clean = _doc("e2e-rollback", "")
        clean["spec"]["mcp_servers"] = []
        v2 = await app.client.put("/manifests/e2e-rollback", json={"manifest": clean}, headers=_bearer(ACME))
        assert v2.status_code == 200, v2.text
        # A new version is not live until activated; the clean one activates like any other.
        live = await app.client.post(
            "/manifests/e2e-rollback/rollback", json={"version": 2}, headers=_bearer(ACME)
        )
        assert live.status_code == 200, live.text

        rollback = await app.client.post(
            "/manifests/e2e-rollback/rollback", json={"version": 1}, headers=_bearer(ACME)
        )
        assert rollback.status_code == 422, rollback.text
        assert "UNLISTED" in rollback.json()["detail"]
        versions = await _versions(app, "e2e-rollback", ACME)
    assert {v["version"]: v["active"] for v in versions} == {2: True, 1: False}


async def test_mcp_auth_resolves_for_the_callers_tenant_and_no_other(
    boot: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `resolve_outbound_secrets` half of the tenant wiring (the test above pins the
    `github_publish` half). The MCP server is faked at the discovery call, the one place a compile
    dials it, so what reached it is observable: the resolved value for acme, nothing for globex."""
    from felix.manifests.store import put_version
    from felix.mcp import client
    from felix_ai.providers.scripted import ScriptedTurn

    backend = install_recording_secrets(monkeypatch, ACME_DOCS_TOKEN="acme-docs-value")
    dialled: list[str] = []

    async def fake_list(ref: Any, *, allow_http: bool = False, handshake: Any = None) -> list[Any]:
        dialled.append(ref.auth)
        return []

    monkeypatch.setattr(client, "list_remote_tools", fake_list)
    env = _keys(
        FELIX_SECRETS_BACKEND=BACKEND,
        FELIX_MANIFEST_SECRETS=json.dumps({"ACME_DOCS_TOKEN": ["acme"]}),
    )
    manifest = parse_manifest(_doc("e2e-mcp", "secret:ACME_DOCS_TOKEN"))
    body = {"manifest": "e2e-mcp", "messages": [{"role": "user", "content": "hi"}]}
    async with boot([ScriptedTurn(content="ok")], env=env) as app:
        for tenant in ("acme", "globex"):
            await put_version(app.settings, tenant, "e2e-mcp", manifest)

        acme = await app.client.post("/chat", json=body, headers=_bearer(ACME))
        assert acme.status_code == 200, acme.text
        assert backend.calls == ["ACME_DOCS_TOKEN"]
        assert dialled == ["acme-docs-value"]

        globex = await app.client.post("/chat", json=body, headers=_bearer(GLOBEX))
        assert globex.status_code == 422, globex.text
        assert backend.calls == ["ACME_DOCS_TOKEN"]
        assert dialled == ["acme-docs-value"]
