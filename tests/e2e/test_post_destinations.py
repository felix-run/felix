"""A scheduled job posts its brief to a shared destination once, however often it fires.

Through the production app: a job written over `PUT /jobs/{name}` and fired by
`POST /jobs/{name}/run`, the manifest's `spec.destinations` compiled into `post` with the real
governance stack, the scripted model calling it, and the delivery reaching a real loopback receiver
through the egress guard as a signed webhook. Nothing between the route and the receiver is
replaced, so what is asserted is what a receiver sees: one request for one edition, whether the job
fired once or twice -- and, when a delivery's outcome was unknown, no second attempt at all.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

from felix.manifests.loader import parse_manifest
from felix_ai.providers.scripted import ScriptedTurn
from felix_ai.types import ToolCall

from tests.support.webhook_receiver import SECRET, receiver

KEYS = {"sk-e2e-alice": {"tenant_id": "default", "sub": "alice", "scopes": ["jobs:write", "jobs:read"]}}
ALICE = {"Authorization": "Bearer sk-e2e-alice"}
EDITION = "brief:2026-10-29"
BRIEF = (
    "Morning brief: two deploys, one incident. Details https://status.example.invalid/i/42 "
    "(mirror: https://elsewhere.example.invalid/i/42)"
)
# What the receiver gets: the registration keeps links to `status.example.invalid` only.
POSTED = (
    "Morning brief: two deploys, one incident. Details https://status.example.invalid/i/42 "
    "(mirror: [link removed]"
)


def _env(url: str) -> dict[str, str]:
    return {
        "FELIX_AUTH_MODE": "api_key",
        "FELIX_AUTH_API_KEYS": json.dumps(KEYS),
        "FELIX_WEBHOOK_ENDPOINTS": json.dumps(
            {"ops": {"url": url, "secret": SECRET, "tenants": ["default"]}}
        ),
        "FELIX_DESTINATIONS": json.dumps(
            {
                "team-brief": {
                    "kind": "webhook",
                    "endpoint": "ops",
                    "tenants": ["default"],
                    "link_hosts": ["status.example.invalid"],
                }
            }
        ),
    }


def _manifest() -> Any:
    spec = {
        "pattern": "react",
        "destinations": {"ids": ["team-brief"]},
    }
    return parse_manifest(
        {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": "briefer"}, "spec": spec}
    )


def _firing(call_id: str) -> list[ScriptedTurn]:
    """One firing: the model posts the day's brief, then answers."""
    args = {"destination": "team-brief", "edition": EDITION, "title": "Morning brief", "text": BRIEF}
    return [
        ScriptedTurn(tool_calls=[ToolCall(id=call_id, name="post", args=args)]),
        ScriptedTurn(content="done"),
    ]


def _tool_result(prompt: list[Any], call_id: str) -> dict[str, Any]:
    """The result of the call `call_id`, from the prompt the model read after making it."""
    (result,) = [
        m for m in prompt if getattr(m, "role", "") == "tool" and getattr(m, "tool_call_id", "") == call_id
    ]
    return json.loads(str(result.content))


async def _fire_twice(app: Any) -> list[dict[str, Any]]:
    """Write the job, fire it twice; the `post` result each firing's model read."""
    written = await app.client.put(
        "/jobs/brief", json={"manifest_id": "briefer", "payload": {"prompt": "post the brief"}}, headers=ALICE
    )
    assert written.status_code == 200, written.text
    results = []
    for call_id in ("c1", "c2"):
        fired = await app.client.post("/jobs/brief/run", headers=ALICE)
        assert fired.status_code == 200, fired.text
        assert fired.json()["status"] == "ok", fired.text
        results.append(_tool_result(app.spy.prompts[-1], call_id))
    return results


async def test_a_jobs_brief_is_posted_once_and_the_next_firing_finds_it_posted(boot: Any) -> None:
    async with (
        receiver([204]) as (url, seen),
        boot([*_firing("c1"), *_firing("c2")], env=_env(url), manifests={"briefer": _manifest()}) as app,
    ):
        first, second = await _fire_twice(app)

    (request,) = seen
    headers, body = request["headers"], request["body"]
    expected = base64.b64encode(
        hmac.new(
            SECRET.encode(),
            f"{headers['webhook-id']}.{headers['webhook-timestamp']}.".encode() + body,
            hashlib.sha256,
        ).digest()
    ).decode()
    assert headers["webhook-signature"] == f"v1,{expected}"
    payload = json.loads(body)
    assert (payload["type"], payload["destination"], payload["edition"]) == (
        "post.created",
        "team-brief",
        EDITION,
    )
    assert payload["text"].startswith(POSTED), "only the registration's link hosts survive"
    assert "elsewhere" not in payload["text"]
    assert payload["unattended"] is True
    assert first == {
        "status": "posted",
        "destination": "team-brief",
        "edition": EDITION,
        "ref": headers["webhook-id"],
    }
    assert second == {**first, "status": "already_posted"}


async def test_an_unknown_outcome_is_maybe_posted_and_the_next_firing_does_not_post(boot: Any) -> None:
    async with (
        receiver([500]) as (url, seen),
        boot([*_firing("c1"), *_firing("c2")], env=_env(url), manifests={"briefer": _manifest()}) as app,
    ):
        first, second = await _fire_twice(app)

    assert len(seen) == 1, "a delivery whose outcome is unknown is never attempted again"
    assert first["error"] == "maybe_posted" and "a person must check" in first["detail"]
    # The ledger's own refusal, before any delivery -- not the receiver's 500 again.
    assert second["error"] == "maybe_posted"
    assert second["detail"].startswith(f"edition '{EDITION}' was already sent to team-brief"), second


async def test_a_manifest_naming_a_destination_not_open_to_its_tenant_is_refused(boot: Any) -> None:
    env = _env("https://hooks.example.invalid/h")
    env["FELIX_DESTINATIONS"] = json.dumps(
        {"team-brief": {"kind": "webhook", "endpoint": "ops", "tenants": ["other"]}}
    )
    async with boot(env=env, manifests={"briefer": _manifest()}) as app:
        resp = await app.client.post(
            "/chat",
            json={"manifest": "briefer", "messages": [{"role": "user", "content": "go"}]},
            headers=ALICE,
        )
    assert resp.status_code == 422, resp.text
    assert "unknown destination: team-brief" in resp.text
