"""A scheduled task tells the person who scheduled it something, on their own browser and no one else's.

The chain no unit test holds end to end: two people subscribe through the real
`POST /push/subscriptions`; `spec.notify` binds `notify` beside `schedule_task`; alice schedules a
task; `POST /jobs/{name}/run` fires it as `cron` on her behalf; the scripted model calls `notify`
from the firing; and the one push that leaves -- decrypted as alice's browser would -- carries the
redacted text and the thread she scheduled from, while bob, subscribed in the same tenant, gets
nothing. A call past `max_per_run` is refused.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from felix.manifests.loader import parse_manifest
from felix_ai.providers.scripted import ScriptedTurn
from felix_ai.types import ToolCall

from tests.support.push_fake import Browser, capture_pushes, vapid_key

KEYS = {
    "sk-e2e-alice": {"tenant_id": "default", "sub": "alice", "scopes": ["jobs:write", "approvals:read"]},
    "sk-e2e-bob": {"tenant_id": "default", "sub": "bob", "scopes": ["approvals:read"]},
}
ALICE = {"Authorization": "Bearer sk-e2e-alice"}
BOB = {"Authorization": "Bearer sk-e2e-bob"}
ALICE_PHONE = "https://web.push.apple.com/e2e-alice-phone"
BOB_LAPTOP = "https://fcm.googleapis.com/fcm/send/e2e-bob-laptop"
SECRET = "sk-planted-e2e-not-a-real-key-41c9"


def _env() -> dict[str, str]:
    return {
        "FELIX_AUTH_MODE": "api_key",
        "FELIX_AUTH_API_KEYS": json.dumps(KEYS),
        "FELIX_PUSH_VAPID_PRIVATE_KEY": vapid_key(),
        "FELIX_PUSH_VAPID_SUBJECT": "mailto:ops@example.invalid",
    }


def _manifest() -> Any:
    return parse_manifest(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": "e2e-notify"},
            "spec": {
                "pattern": "react",
                "scheduling": {"approval": False},
                "notify": {"max_per_run": 1},
            },
        }
    )


def _when() -> str:
    at = (datetime.now(UTC) + timedelta(days=1)).replace(microsecond=0)
    return "at:" + at.isoformat().replace("+00:00", "Z")


def _call(call_id: str, name: str, **args: Any) -> ScriptedTurn:
    return ScriptedTurn(tool_calls=[ToolCall(id=call_id, name=name, args=args)])


def _tool_result(prompt: list[Any]) -> dict[str, Any]:
    return json.loads(next(str(m.content) for m in reversed(prompt) if getattr(m, "role", "") == "tool"))


async def _subscribe(app: Any, headers: dict[str, str], browser: Browser) -> None:
    res = await app.client.post("/push/subscriptions", json=browser.subscription(), headers=headers)
    assert res.status_code == 200, res.text


async def test_a_scheduled_task_notifies_only_the_person_who_scheduled_it(
    boot: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = capture_pushes(monkeypatch)
    script = [
        _call("c1", "schedule_task", when=_when(), prompt="Tell me when 1.4 ships."),
        ScriptedTurn(content="scheduled"),
        # The firing: one notice, then a second the run's limit refuses.
        _call("c2", "notify", title="1.4 shipped", message=f"Released with key {SECRET}."),
        _call("c3", "notify", title="again", message="One more."),
        ScriptedTurn(content="told her"),
    ]
    async with boot(script, env=_env(), manifests={"e2e-notify": _manifest()}) as app:
        # After boot: hydration at startup replaces the masking list.
        monkeypatch.setattr("felix.secrets._resolved_secret_values", [SECRET])
        alice, bob = Browser(ALICE_PHONE), Browser(BOB_LAPTOP)
        await _subscribe(app, ALICE, alice)
        await _subscribe(app, BOB, bob)

        chat = await app.client.post(
            "/chat",
            headers=ALICE,
            json={
                "manifest": "e2e-notify",
                "thread_id": "e2e-notify",
                "messages": [{"role": "user", "content": "Let me know when 1.4 ships."}],
            },
        )
        assert chat.status_code == 200, chat.text
        scheduled = _tool_result(app.spy.prompts[1])
        assert scheduled["status"] == "scheduled", scheduled

        fired = await app.client.post(f"/jobs/{scheduled['name']}/run", headers=ALICE)
        assert fired.status_code == 200 and fired.json()["status"] == "ok", fired.text

        assert _tool_result(app.spy.prompts[3]) == {"delivered": 1, "channels": ["push"]}
        assert _tool_result(app.spy.prompts[4])["error"] == "rate_limited"

        assert [p["endpoint"] for p in sent] == [ALICE_PHONE], "bob is in the tenant and gets nothing"
        assert alice.decrypt(sent[0]["body"]) == {
            "kind": "notice",
            "title": "1.4 shipped",
            "body": "Released with key [REDACTED].",
            # Where alice asked, not the firing's own job thread.
            "thread_id": "default:e2e-notify",
        }


class WorkerDied(BaseException):
    """What a SIGKILL looks like from inside the step: nothing after it runs."""


async def test_a_durable_run_notifies_its_starter_once_per_resume(
    boot: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A durable run resumed after a crash runs as `fiber` for alice, so its notices are hers. The
    per-run count starts again on the resume -- each resume is a fresh context -- which is what the
    per-hour limit is there to bound; the call made before the crash is not repeated."""
    from felix.durability import fibers as F

    sent = capture_pushes(monkeypatch)
    manifest = parse_manifest(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": "e2e-notify-durable"},
            "spec": {"pattern": "react", "execution": {"mode": "durable"}, "notify": {"max_per_run": 1}},
        }
    )
    script = [
        _call("c1", "notify", title="started", message="On it."),
        ScriptedTurn(error=WorkerDied()),
        _call("c2", "notify", title="resumed", message="Back at it."),
        ScriptedTurn(content="done"),
    ]
    async with boot(script, env=_env(), manifests={"e2e-notify-durable": manifest}) as app:
        alice, bob = Browser(ALICE_PHONE), Browser(BOB_LAPTOP)
        await _subscribe(app, ALICE, alice)
        await _subscribe(app, BOB, bob)
        queued = await app.client.post(
            "/chat",
            headers=ALICE,
            json={"manifest": "e2e-notify-durable", "messages": [{"role": "user", "content": "Go."}]},
        )
        assert queued.status_code == 202, queued.text
        token = queued.json()["resume_token"]

        with pytest.raises(WorkerDied):
            await F.resume_due_fibers(app.settings)
        F._memory_fibers[("default", token)]["lease_until"] = 0
        await F.resume_due_fibers(app.settings)
        run = (await app.client.get(f"/chat/runs/{token}", headers=ALICE)).json()

    assert run["status"] == "completed", run
    assert [p["endpoint"] for p in sent] == [ALICE_PHONE, ALICE_PHONE], "alice's, never bob's or `fiber`'s"
    assert [alice.decrypt(p["body"])["title"] for p in sent] == ["started", "resumed"]
