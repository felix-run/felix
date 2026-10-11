"""A manifest naming a credential its tenant may not use is the caller's 422, not an outage."""

from __future__ import annotations

import json
from typing import Any

from felix.manifests.loader import parse_manifest


def _agent() -> Any:
    spec = {
        "pattern": "react",
        "auth": {"inbound": {"allow_anonymous": True}},
        "network": {"allow_hosts": ["api.example.com"], "credentials": ["github"]},
        "http_tools": [{"name": "fetch", "allow_any_host": True}],
    }
    return parse_manifest(
        {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": "e2e-keyed"}, "spec": spec}
    )


async def test_a_credential_registered_for_another_tenant_is_refused_by_id(boot: Any) -> None:
    registry = {"github": {"host": "api.example.com", "secret": "never-sent-0000", "tenants": ["acme"]}}
    env = {"FELIX_EGRESS_CREDENTIALS": json.dumps(registry)}
    async with boot(env=env, manifests={"e2e-keyed": _agent()}) as app:
        resp = await app.client.post(
            "/chat", json={"manifest": "e2e-keyed", "messages": [{"role": "user", "content": "hi"}]}
        )
        assert app.spy.prompts == []
    assert resp.status_code == 422
    assert resp.json()["detail"] == "unknown egress credential: github"
