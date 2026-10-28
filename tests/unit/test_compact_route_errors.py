"""`POST /chat/compact` answers a model it cannot build as a turn of the same manifest would.

The summariser's model is built from `spec.model` before anything is written. A route the
deployment has not configured -- here Workers AI with no `account_id` -- raised out of the route as
a 500 and an operator's misconfiguration looked like a server fault. It is the 503 a chat turn
answers (`_http_from_invoke_prep`), and the thread is left as it was.
"""

from __future__ import annotations

import json

from felix.manifests.loader import parse_manifest

from tests.support.factories import app_client, make_settings


async def test_an_unconfigured_provider_is_a_503_and_the_thread_is_untouched() -> None:
    from felix.manifests import store as manifest_store
    from felix.session.thread_state import get_thread_meta

    routes = {"unconfigured": {"provider": "workers_ai", "model": "@cf/meta/llama"}}
    settings = make_settings(
        model_routes=json.dumps(routes), model_provider_options="", auth_mode="none", redis_url=""
    )
    manifest = parse_manifest(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": "cf"},
            "spec": {"model": {"id": "unconfigured"}, "auth": {"inbound": {"allow_anonymous": True}}},
        }
    )
    await manifest_store.put_version(settings, "default", "cf", manifest)

    async with app_client(settings) as client:
        resp = await client.post("/chat/compact", json={"thread_id": "t1", "manifest": "cf"})

    assert resp.status_code == 503, resp.text
    assert "account_id" in resp.json()["detail"]
    meta = await get_thread_meta(settings=settings, tenant_id="default", thread_id="default:t1")
    assert meta["phase"] == "idle"
