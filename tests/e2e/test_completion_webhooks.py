"""A durable run announced to its webhook, through the stack and both worker sweeps.

`POST /chat` enqueues the run with its endpoints validated for the tenant; the fiber sweep runs
it to a terminal status; the webhook sweep delivers it. Nothing is stubbed between the route and
the receiver, which is the only way to see that the endpoint named on the manifest is the one
the finished run reaches, carrying the answer the model gave.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from felix.manifests.loader import parse_manifest
from felix_ai.providers.scripted import ScriptedTurn

from tests.unit.test_completion_webhooks import SECRET, receiver


def _durable(webhooks: list[str]) -> Any:
    spec = {
        "pattern": "react",
        "auth": {"inbound": {"allow_anonymous": True}},
        "execution": {"mode": "durable", "webhooks": webhooks},
    }
    return parse_manifest(
        {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": "e2e-hooked"}, "spec": spec}
    )


async def test_a_durable_run_is_announced_to_its_webhook_when_it_finishes(boot: Any) -> None:
    from felix.durability.fibers import resume_due_fibers
    from felix.durability.webhooks import deliver_due_webhooks

    async with receiver([204]) as (url, seen):
        env = {"FELIX_WEBHOOK_ENDPOINTS": json.dumps({"ops": {"url": url, "secret": SECRET, "tenants": "*"}})}
        script = [ScriptedTurn(content="the durable answer")]
        async with boot(script, env=env, manifests={"e2e-hooked": _durable(["ops"])}) as app:
            accepted = await app.client.post(
                "/chat", json={"manifest": "e2e-hooked", "messages": [{"role": "user", "content": "go"}]}
            )
            assert accepted.status_code == 202, accepted.text
            token = accepted.json()["resume_token"]

            assert await deliver_due_webhooks(app.settings) == 0, "not before the run finishes"
            await resume_due_fibers(app.settings)
            assert await deliver_due_webhooks(app.settings) == 1

            run = (await app.client.get(f"/chat/runs/{token}")).json()

    [delivery] = seen
    payload = json.loads(delivery["body"])
    assert payload["type"] == "run.completed"
    assert payload["run"]["resume_token"] == token
    assert payload["run"]["final"]["content"] == "the durable answer"
    assert delivery["headers"]["webhook-signature"].startswith("v1,")
    assert run["status"] == "completed" and run["webhooks"] == {"ops": "delivered"}


async def test_a_manifest_naming_an_unregistered_endpoint_is_refused_at_enqueue(boot: Any) -> None:
    env = {
        "FELIX_WEBHOOK_ENDPOINTS": json.dumps(
            {"ops": {"url": "https://x.test/h", "secret": SECRET, "tenants": "*"}}
        )
    }
    async with boot(env=env, manifests={"e2e-hooked": _durable(["elsewhere"])}) as app:
        resp = await app.client.post(
            "/chat", json={"manifest": "e2e-hooked", "messages": [{"role": "user", "content": "go"}]}
        )
    assert resp.status_code == 422, resp.text
    assert "unknown webhook endpoint: elsewhere" in resp.text


def test_webhooks_on_a_run_that_answers_its_own_request_are_refused() -> None:
    from felix.manifests.loader import ManifestParseError

    with pytest.raises(ManifestParseError, match=r"execution\.mode: durable"):
        parse_manifest(
            {
                "apiVersion": "felix/v1",
                "kind": "Agent",
                "metadata": {"name": "x"},
                "spec": {"execution": {"webhooks": ["ops"]}},
            }
        )
