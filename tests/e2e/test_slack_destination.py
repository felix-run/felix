"""A scheduled job posts its brief into a Slack conversation once, however often it fires.

Through the production app: the `slack` kind as `felix_channels.register` installs it (the entry
point `load_optional_plugins` runs once per process; unit tests that install it themselves put
back what was there with `monkeypatch.setitem`), a `FELIX_CHANNELS` app
and a `slack` destination through it, a job written over `PUT /jobs/{name}` and fired twice by
`POST /jobs/{name}/run`, the scripted model calling `post` with the same edition each time. Slack is
replaced at the transport only (`tests/support/slack_fake.py`), so what is asserted is the request
Slack would get: one `chat.postMessage`, with the app's token, plain text that pings nobody.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from felix.manifests.loader import parse_manifest
from felix_ai.providers.scripted import ScriptedTurn
from felix_ai.types import ToolCall

from tests.support.optional_deps import require_optional
from tests.support.slack_fake import POST_MESSAGE_URL, FakeSlack

KEYS = {"sk-e2e-alice": {"tenant_id": "default", "sub": "alice", "scopes": ["jobs:write", "jobs:read"]}}
ALICE = {"Authorization": "Bearer sk-e2e-alice"}
EDITION = "brief:2026-10-29"
CONVERSATION = "C0CHAN01"
TOKEN = "planted-e2e-bot-token-91c3"
BRIEF = "Two deploys, one incident. <!channel> @here"


@pytest.fixture(autouse=True)
def _plugin() -> Any:
    return require_optional("felix_channels", "channels")


@pytest.fixture
def slack(monkeypatch: pytest.MonkeyPatch) -> FakeSlack:
    from felix_channels import slack as slack_mod

    fake = FakeSlack()
    monkeypatch.setattr(slack_mod, "slack_http_client", fake.client)
    monkeypatch.setenv("SLACK_E2E_BOT", TOKEN)
    monkeypatch.setenv("SLACK_E2E_SIGNING", "s" * 32)
    return fake


ENV = {
    "FELIX_AUTH_MODE": "api_key",
    "FELIX_AUTH_API_KEYS": json.dumps(KEYS),
    "FELIX_CHANNELS": json.dumps(
        {
            "acme-slack": {
                "platform": "slack",
                "tenant": "default",
                "manifest": "briefer",
                "team_id": "T0TEAM01",
                "signing_secret": "secret:SLACK_E2E_SIGNING",
                "bot_token": "secret:SLACK_E2E_BOT",
            }
        }
    ),
    "FELIX_DESTINATIONS": json.dumps(
        {
            "team-brief": {
                "kind": "slack",
                "channel": "acme-slack",
                "conversation": CONVERSATION,
                "tenants": ["default"],
            }
        }
    ),
}


def _manifest() -> Any:
    spec = {"pattern": "react", "destinations": {"ids": ["team-brief"]}}
    return parse_manifest(
        {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": "briefer"}, "spec": spec}
    )


def _firing(call_id: str) -> list[ScriptedTurn]:
    args = {"destination": "team-brief", "edition": EDITION, "title": "Morning brief", "text": BRIEF}
    return [
        ScriptedTurn(tool_calls=[ToolCall(id=call_id, name="post", args=args)]),
        ScriptedTurn(content="done"),
    ]


def _tool_result(prompt: list[Any], call_id: str) -> dict[str, Any]:
    (result,) = [
        m for m in prompt if getattr(m, "role", "") == "tool" and getattr(m, "tool_call_id", "") == call_id
    ]
    return json.loads(str(result.content))


async def test_a_jobs_brief_is_posted_to_slack_once_and_the_next_firing_finds_it_posted(
    boot: Any, slack: FakeSlack
) -> None:
    async with boot([*_firing("c1"), *_firing("c2")], env=ENV, manifests={"briefer": _manifest()}) as app:
        written = await app.client.put(
            "/jobs/brief",
            json={"manifest_id": "briefer", "payload": {"prompt": "post the brief"}},
            headers=ALICE,
        )
        assert written.status_code == 200, written.text
        results = []
        for call_id in ("c1", "c2"):
            fired = await app.client.post("/jobs/brief/run", headers=ALICE)
            assert fired.status_code == 200, fired.text
            assert fired.json()["status"] == "ok", fired.text
            results.append(_tool_result(app.spy.prompts[-1], call_id))

    first, second = results
    assert slack.urls == [POST_MESSAGE_URL], "one chat.postMessage for one edition"
    (sent,) = slack.posts
    assert slack.tokens == [f"Bearer {TOKEN}"]
    assert sent["channel"] == CONVERSATION
    assert sent["text"] == "*Morning brief*\nTwo deploys, one incident. &lt;!channel&gt; @here"
    assert (sent["parse"], sent["link_names"], sent["unfurl_links"]) == ("none", False, False)
    ref = f"{CONVERSATION}:1700000000.000200"
    assert first == {"status": "posted", "destination": "team-brief", "edition": EDITION, "ref": ref}
    assert second == {**first, "status": "already_posted"}
    assert TOKEN not in json.dumps(results)


async def test_a_slack_destination_of_another_tenant_fails_the_compile(boot: Any, slack: FakeSlack) -> None:
    env = {
        **ENV,
        "FELIX_CHANNELS": ENV["FELIX_CHANNELS"].replace('"tenant": "default"', '"tenant": "globex"'),
    }
    async with boot(env=env, manifests={"briefer": _manifest()}) as app:
        resp = await app.client.post(
            "/chat",
            json={"manifest": "briefer", "messages": [{"role": "user", "content": "go"}]},
            headers=ALICE,
        )
    assert resp.status_code == 422, resp.text
    assert "names a Slack channel not registered for this tenant" in resp.text
    assert "globex" not in resp.text
    assert slack.posts == []
