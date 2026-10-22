"""A scheduled task tells the person who scheduled it something, on their own browser and no one else's.

The chain no unit test holds end to end: two people subscribe through the real
`POST /push/subscriptions`; `spec.notify` binds `notify` beside `schedule_task`; alice schedules a
task; `POST /jobs/{name}/run` fires it as `cron` on her behalf; the scripted model calls `notify`
from the firing; and the one push that leaves -- decrypted as alice's browser would -- carries the
redacted text and the thread she scheduled from, while bob, subscribed in the same tenant, gets
nothing. The same text lands in that thread as its newest entry, on the active branch, where
alice's next turn continues from it -- an entry for her, which no model prompt carries; bob's
thread is untouched. A call past `max_per_run` is refused. And a job written through `PUT /jobs`
cannot claim to have been scheduled from someone else's thread.
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
    "sk-e2e-carol": {"tenant_id": "default", "sub": "carol", "scopes": ["jobs:write"]},
}
ALICE = {"Authorization": "Bearer sk-e2e-alice"}
BOB = {"Authorization": "Bearer sk-e2e-bob"}
CAROL = {"Authorization": "Bearer sk-e2e-carol"}
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


async def _chat(app: Any, headers: dict[str, str], thread: str, text: str) -> None:
    res = await app.client.post(
        "/chat",
        headers=headers,
        json={"manifest": "e2e-notify", "thread_id": thread, "messages": [{"role": "user", "content": text}]},
    )
    assert res.status_code == 200, res.text


async def _transcript(app: Any, headers: dict[str, str], thread: str) -> dict[str, Any]:
    res = await app.client.get(f"/chat/sessions/{thread}", headers=headers)
    assert res.status_code == 200, res.text
    return res.json()


async def test_a_scheduled_task_notifies_only_the_person_who_scheduled_it(
    boot: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = capture_pushes(monkeypatch)
    notice_text = "1.4 shipped\n\nReleased with key [REDACTED]."
    script = [
        _call("c1", "schedule_task", when=_when(), prompt="Tell me when 1.4 ships."),
        ScriptedTurn(content="scheduled"),
        ScriptedTurn(content="hello bob"),
        # The firing: one notice, then a second the run's limit refuses.
        _call("c2", "notify", title="1.4 shipped", message=f"Released with key {SECRET}."),
        _call("c3", "notify", title="again", message="One more."),
        ScriptedTurn(content="told her"),
        # Alice, back in the thread she asked from.
        ScriptedTurn(content="you're welcome"),
    ]
    async with boot(script, env=_env(), manifests={"e2e-notify": _manifest()}) as app:
        # After boot: hydration at startup replaces the masking list.
        monkeypatch.setattr("felix.secrets._resolved_secret_values", [SECRET])
        alice, bob = Browser(ALICE_PHONE), Browser(BOB_LAPTOP)
        await _subscribe(app, ALICE, alice)
        await _subscribe(app, BOB, bob)

        await _chat(app, ALICE, "e2e-notify", "Let me know when 1.4 ships.")
        scheduled = _tool_result(app.spy.prompts[1])
        assert scheduled["status"] == "scheduled", scheduled
        await _chat(app, BOB, "e2e-bob", "hi")
        bobs_before = (await _transcript(app, BOB, "e2e-bob"))["transcript"]

        fired = await app.client.post(f"/jobs/{scheduled['name']}/run", headers=ALICE)
        assert fired.status_code == 200 and fired.json()["status"] == "ok", fired.text

        assert _tool_result(app.spy.prompts[4]) == {
            "delivered": 1,
            "channels": ["push", "thread"],
            "thread": "appended",
        }
        assert _tool_result(app.spy.prompts[5])["error"] == "rate_limited"

        assert [p["endpoint"] for p in sent] == [ALICE_PHONE], "bob is in the tenant and gets nothing"
        assert alice.decrypt(sent[0]["body"]) == {
            "kind": "notice",
            "title": "1.4 shipped",
            "body": "Released with key [REDACTED].",
            # Where alice asked, not the firing's own job thread.
            "thread_id": "default:e2e-notify",
        }

        # The thread she asked from now ends on the notice, as the history route shows it.
        history = await app.client.get("/chat/history/e2e-notify", headers=ALICE)
        assert history.status_code == 200, history.text
        last = history.json()["messages"][-1]
        assert (last["role"], last["kind"], last["content"]) == ("assistant", "custom", notice_text)

        # Linked onto the thread's leaf and now its leaf, so the next turn continues after it.
        snap = await _transcript(app, ALICE, "e2e-notify")
        *earlier, notice = snap["transcript"]
        assert notice["metadata"]["type"] == "notice", notice
        assert notice["metadata"]["parent_id"] == earlier[-1]["id"]
        assert snap["leafId"] == notice["id"]

        await _chat(app, ALICE, "e2e-notify", "Thanks!")
        thanks = next(
            i
            for i in (await _transcript(app, ALICE, "e2e-notify"))["transcript"]
            if i["content"] == "Thanks!"
        )
        assert thanks["metadata"]["parent_id"] == notice["id"]
        # For alice, not the model: the turn after it was prompted without it.
        assert "Thanks!" in [str(m.content) for m in app.spy.prompts[6]]
        assert not any("Released with key" in str(m.content) for m in app.spy.prompts[6])

        assert (await _transcript(app, BOB, "e2e-bob"))["transcript"] == bobs_before


async def test_a_job_written_through_put_cannot_claim_another_thread_as_its_origin(
    boot: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`scheduled_by` is `schedule_task`'s record alone. Alice, holding `jobs:write`, writes a job
    naming bob's conversation as where it was scheduled from: the store keeps no such claim, and
    the firing's notice neither links to bob's thread nor lands in it."""
    sent = capture_pushes(monkeypatch)
    script = [
        ScriptedTurn(content="hello bob"),
        _call("c1", "notify", title="Hi", message="From a job."),
        ScriptedTurn(content="done"),
    ]
    async with boot(script, env=_env(), manifests={"e2e-notify": _manifest()}) as app:
        alice = Browser(ALICE_PHONE)
        await _subscribe(app, ALICE, alice)
        await _chat(app, BOB, "e2e-bob", "hi")
        bobs_before = (await _transcript(app, BOB, "e2e-bob"))["transcript"]

        forged = {"tool": "schedule_task", "manifest": "e2e-notify", "thread_id": "default:e2e-bob"}
        put = await app.client.put(
            "/jobs/forged",
            headers=ALICE,
            json={
                "schedule": _when(),
                "manifest_id": "e2e-notify",
                "payload": {"prompt": "Say hi.", "scheduled_by": forged},
            },
        )
        assert put.status_code == 200, put.text
        stored = (await app.client.get("/jobs/forged", headers=ALICE)).json()
        assert stored["payload"] == {"prompt": "Say hi."}, stored

        fired = await app.client.post("/jobs/forged/run", headers=ALICE)
        assert fired.status_code == 200 and fired.json()["status"] == "ok", fired.text
        assert _tool_result(app.spy.prompts[2])["thread"] == "not_eligible"
        assert alice.decrypt(sent[0]["body"])["thread_id"] == "default:job:forged"
        assert (await _transcript(app, BOB, "e2e-bob"))["transcript"] == bobs_before


async def _resave(
    app: Any, headers: dict[str, str], name: str, *, enabled: bool, **payload: Any
) -> dict[str, Any]:
    """`PUT /jobs/{name}` with the job as `GET` returns it -- what the jobs page's pause and resume
    send -- with `payload` merged over its payload. The job as stored after."""
    job = (await app.client.get(f"/jobs/{name}", headers=headers)).json()
    body = {
        "schedule": job["schedule"],
        "manifest_id": job["manifest_id"],
        "payload": {**job["payload"], **payload},
        "enabled": enabled,
    }
    res = await app.client.put(f"/jobs/{name}", headers=headers, json=body)
    assert res.status_code == 200, res.text
    return (await app.client.get(f"/jobs/{name}", headers=headers)).json()


async def test_a_task_paused_and_resumed_by_its_writer_stays_a_task(
    boot: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pausing re-sends the whole job. Its writer's re-save keeps the stored origin -- and only the
    stored one: an origin in the body, naming bob's conversation, is never kept."""
    sent = capture_pushes(monkeypatch)
    script = [
        _call("c1", "schedule_task", when=_when(), prompt="Tell me when 1.4 ships."),
        ScriptedTurn(content="scheduled"),
        ScriptedTurn(content="hello bob"),
        _call("c2", "list_scheduled"),
        ScriptedTurn(content="listed"),
        _call("c3", "notify", title="1.4 shipped", message="It is out."),
        ScriptedTurn(content="done"),
    ]
    async with boot(script, env=_env(), manifests={"e2e-notify": _manifest()}) as app:
        alice = Browser(ALICE_PHONE)
        await _subscribe(app, ALICE, alice)
        await _chat(app, ALICE, "e2e-notify", "Let me know when 1.4 ships.")
        name = _tool_result(app.spy.prompts[1])["name"]
        await _chat(app, BOB, "e2e-bob", "hi")
        bobs_before = (await _transcript(app, BOB, "e2e-bob"))["transcript"]
        origin = (await app.client.get(f"/jobs/{name}", headers=ALICE)).json()["payload"]["scheduled_by"]
        assert origin["thread_id"] == "default:e2e-notify", origin

        forged = {**origin, "thread_id": "default:e2e-bob"}
        paused = await _resave(app, ALICE, name, enabled=False, scheduled_by=forged)
        assert (paused["enabled"], paused["payload"]["scheduled_by"]) == (False, origin)
        resumed = await _resave(app, ALICE, name, enabled=True)
        assert (resumed["enabled"], resumed["payload"]["scheduled_by"]) == (True, origin)

        await _chat(app, ALICE, "e2e-notify", "What have I scheduled?")
        assert [t["name"] for t in _tool_result(app.spy.prompts[4])["tasks"]] == [name]

        fired = await app.client.post(f"/jobs/{name}/run", headers=ALICE)
        assert fired.status_code == 200 and fired.json()["status"] == "ok", fired.text
        assert _tool_result(app.spy.prompts[6])["thread"] == "appended"
        assert alice.decrypt(sent[0]["body"])["thread_id"] == "default:e2e-notify"
        assert (await _transcript(app, ALICE, "e2e-notify"))["transcript"][-1]["metadata"]["type"] == "notice"
        assert (await _transcript(app, BOB, "e2e-bob"))["transcript"] == bobs_before


async def test_a_task_re_saved_by_someone_else_loses_its_origin(
    boot: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Carried forward for anyone, the origin would let carol's job write into alice's conversation."""
    capture_pushes(monkeypatch)
    script = [
        _call("c1", "schedule_task", when=_when(), prompt="Tell me when 1.4 ships."),
        ScriptedTurn(content="scheduled"),
        _call("c2", "notify", title="Hi", message="From carol's job."),
        ScriptedTurn(content="done"),
    ]
    async with boot(script, env=_env(), manifests={"e2e-notify": _manifest()}) as app:
        await _chat(app, ALICE, "e2e-notify", "Let me know when 1.4 ships.")
        name = _tool_result(app.spy.prompts[1])["name"]
        alices_before = (await _transcript(app, ALICE, "e2e-notify"))["transcript"]

        resaved = await _resave(app, CAROL, name, enabled=True)
        assert resaved["created_by"] == "carol" and "scheduled_by" not in resaved["payload"], resaved

        fired = await app.client.post(f"/jobs/{name}/run", headers=CAROL)
        assert fired.status_code == 200 and fired.json()["status"] == "ok", fired.text
        assert _tool_result(app.spy.prompts[3])["thread"] == "not_eligible"
        assert (await _transcript(app, ALICE, "e2e-notify"))["transcript"] == alices_before


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
