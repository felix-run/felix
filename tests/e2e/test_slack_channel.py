"""Slack through the real stack: a signed event starts a durable run, and the answer goes back.

`create_application()` with `FELIX_AUTH_MODE=api_key` and no bearer on Slack's requests, the
`felix-channels` plugin loaded the way production loads it -- through its `felix.plugins` entry
point -- and Slack's Web API faked at the transport (`tests/support/slack_fake.py`). What is pinned
is what a Slack user and an operator are promised: a mention or a DM that verifies starts one run,
as its sender, on the thread for its Slack conversation, and the worker posts the answer under it,
escaped and unfurled by nobody; a request that does not verify, repeats, is not for this app, comes
from a bot or another organisation, or is over a limit starts none.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest
from felix.manifests.loader import parse_manifest
from felix_ai.providers.scripted import ScriptedTurn

from tests.support.e2e import sent_turns
from tests.support.optional_deps import require_optional
from tests.support.slack_fake import POST_MESSAGE_URL, FakeSlack, signed_headers, slack_signature

SIGNING = "s" * 32
TEAM, USER, CHANNEL = "T0TEAM01", "U0USER01", "C0CHAN01"
API_KEYS = json.dumps({"k": {"tenant_id": "default", "sub": "e2e", "scopes": ["admin"]}})
UNAUTHORIZED = {"error": "unauthorized", "reason": "missing_credentials"}
OK = {"ok": True}
NO_UNFURL = {"parse": "none", "link_names": False, "unfurl_links": False, "unfurl_media": False}


def _assistant(**auth: Any) -> Any:
    spec = {
        "pattern": "react",
        "system_prompt": {"inline": "You help."},
        "auth": {"inbound": {"allow_anonymous": False, **auth}},
    }
    meta = {"name": "e2e-assistant"}
    return parse_manifest({"apiVersion": "felix/v1", "kind": "Agent", "metadata": meta, "spec": spec})


ASSISTANT = _assistant()


@pytest.fixture(autouse=True)
def _plugin() -> Any:
    return require_optional("felix_channels", "channels")


@pytest.fixture
def slack(monkeypatch: pytest.MonkeyPatch) -> FakeSlack:
    from felix_channels import slack as slack_mod

    fake = FakeSlack()
    monkeypatch.setattr(slack_mod, "slack_http_client", fake.client)
    return fake


def _entry(**spec: Any) -> dict[str, Any]:
    return {
        "platform": "slack",
        "tenant": "default",
        "manifest": "e2e-assistant",
        "team_id": TEAM,
        "signing_secret": "secret:SLACK_E2E_SIGNING",
        "bot_token": "secret:SLACK_E2E_BOT",
        **spec,
    }


def _env(registry: dict[str, Any] | None = None, **spec: Any) -> dict[str, str]:
    return {
        "FELIX_AUTH_MODE": "api_key",
        "FELIX_AUTH_API_KEYS": API_KEYS,
        "FELIX_CHANNELS": json.dumps(registry or {"acme": _entry(**spec)}),
        "SLACK_E2E_SIGNING": SIGNING,
        "SLACK_E2E_BOT": "xoxb-e2e-bot-token",
    }


def _event(text: str = "<@U0BOT001> what's up?", *, event_id: str = "Ev0001", **event: Any) -> bytes:
    inner = {"type": "app_mention", "user": USER, "channel": CHANNEL, "ts": "1700000000.000100", "text": text}
    body = {"type": "event_callback", "team_id": TEAM, "event_id": event_id, "event": {**inner, **event}}
    return json.dumps(body).encode()


async def _post(app: Any, body: bytes, headers: dict[str, str] | None = None, path: str = "acme") -> Any:
    headers = headers if headers is not None else signed_headers(body, SIGNING)
    return await app.client.post(f"/channels/slack/{path}/events", content=body, headers=headers)


async def _runs() -> list[dict[str, Any]]:
    from felix.durability import fibers as F
    from felix.durability.runs import DURABLE_CHAT_KIND

    return [r for r in F._memory_fibers.values() if r.get("kind") == DURABLE_CHAT_KIND]


async def _drain(app: Any) -> None:
    from felix.durability import fibers as F

    for _ in range(10):
        if not await F.resume_due_fibers(app.settings):
            return
    raise AssertionError("the run did not end")


async def test_a_mention_starts_a_run_as_its_sender_and_the_answer_is_posted_in_its_thread(
    boot: Any, slack: FakeSlack
) -> None:
    async with boot(
        [ScriptedTurn(content="All good.")], env=_env(), manifests={"e2e-assistant": ASSISTANT}
    ) as app:
        resp = await _post(app, _event())
        assert resp.status_code == 200 and resp.json() == OK
        [run] = await _runs()
        assert run["thread_id"] == f"default:channel:acme:{CHANNEL}:1700000000.000100"
        assert run["state_json"]["auth"]["principal_sub"] == f"slack:{TEAM}:{USER}"
        assert slack.posts == [], "nothing is posted until the run ends"
        await _drain(app)
        [prompt] = app.spy.prompts
    assert [m.content for m in sent_turns(prompt)] == ["what's up?"], "the bot mention is stripped"
    assert slack.urls == [POST_MESSAGE_URL]
    assert slack.posts == [
        {"channel": CHANNEL, "text": "All good.", "thread_ts": "1700000000.000100", **NO_UNFURL}
    ]
    assert slack.tokens == ["Bearer xoxb-e2e-bot-token"]


async def test_an_answer_cannot_ping_the_channel_or_disguise_a_link(boot: Any, slack: FakeSlack) -> None:
    """Slack reads `<...>` as control sequences: escaped, the answer is only text."""
    answer = "Done <!channel> see <https://evil.example/?d=secret|your report> & thanks"
    async with boot(
        [ScriptedTurn(content=answer)], env=_env(), manifests={"e2e-assistant": ASSISTANT}
    ) as app:
        await _post(app, _event())
        await _drain(app)
    [post] = slack.posts
    assert (
        post["text"]
        == "Done &lt;!channel&gt; see &lt;https://evil.example/?d=secret|your report&gt; &amp; thanks"
    )
    assert {k: post[k] for k in NO_UNFURL} == NO_UNFURL


async def test_a_reply_in_the_thread_continues_the_same_conversation(boot: Any, slack: FakeSlack) -> None:
    script = [ScriptedTurn(content="First."), ScriptedTurn(content="Second.")]
    async with boot(script, env=_env(), manifests={"e2e-assistant": ASSISTANT}) as app:
        await _post(app, _event("<@U0BOT001> one"))
        await _drain(app)
        reply = _event(
            "<@U0BOT001> two", event_id="Ev0002", ts="1700000000.000300", thread_ts="1700000000.000100"
        )
        await _post(app, reply)
        await _drain(app)
        second = app.spy.prompts[1]
    assert {r["thread_id"] for r in await _runs()} == {f"default:channel:acme:{CHANNEL}:1700000000.000100"}
    assert [m.content for m in sent_turns(second, ("user", "assistant"))] == [
        "one",
        "First.",
        "two",
    ]
    assert [p["thread_ts"] for p in slack.posts] == ["1700000000.000100"] * 2


async def test_a_dm_is_one_running_conversation_and_a_dm_thread_its_own(boot: Any, slack: FakeSlack) -> None:
    dm = {"type": "message", "channel_type": "im", "channel": "D0DM0001"}
    script = [ScriptedTurn(content="Hi."), ScriptedTurn(content="In the thread.")]
    async with boot(script, env=_env(), manifests={"e2e-assistant": ASSISTANT}) as app:
        await _post(app, _event("hello", **dm))
        await _drain(app)
        await _post(app, _event("more", event_id="Ev0002", ts="1.5", thread_ts="1.2", **dm))
        await _drain(app)
        threads = sorted(r["thread_id"] for r in await _runs())
    assert threads == ["default:channel:acme:D0DM0001:1.2", "default:channel:acme:D0DM0001:dm"]
    assert slack.posts[0] == {"channel": "D0DM0001", "text": "Hi.", **NO_UNFURL}, "the main line, in place"
    assert slack.posts[1]["thread_ts"] == "1.2"


@pytest.mark.parametrize("challenge", [123, None, "x" * 257])
async def test_slacks_url_verification_is_answered_once_signed_and_well_formed(
    boot: Any, challenge: Any
) -> None:
    good = json.dumps({"type": "url_verification", "challenge": "abc123"}).encode()
    bad = json.dumps({"type": "url_verification", "challenge": challenge}).encode()
    async with boot([], env=_env(), manifests={"e2e-assistant": ASSISTANT}) as app:
        ok = await _post(app, good)
        unsigned = await _post(app, good, headers={"content-type": "application/json"})
        malformed = await _post(app, bad)
    assert ok.status_code == 200 and ok.json() == {"challenge": "abc123"}
    assert unsigned.status_code == 401 and unsigned.json() == {"detail": "invalid_signature"}
    assert malformed.status_code == 400 and malformed.json() == {"detail": "invalid_payload"}


async def test_a_request_that_does_not_verify_starts_nothing(boot: Any, slack: FakeSlack) -> None:
    body = _event()
    async with boot([], env=_env(), manifests={"e2e-assistant": ASSISTANT}) as app:
        refused = [
            await _post(app, body, signed_headers(body, "x" * 32)),
            await _post(app, body + b" ", signed_headers(body, SIGNING)),
            await _post(app, body, signed_headers(body, SIGNING, ts=int(time.time()) - 301)),
            await _post(app, body, path="nope"),
        ]
        runs = await _runs()
    assert [(r.status_code, r.json()) for r in refused] == [(401, {"detail": "invalid_signature"})] * 4
    assert runs == [] and slack.posts == []


async def test_only_slacks_own_path_is_open_without_a_bearer(boot: Any) -> None:
    """The exemption is `/channels/...` and nothing beside it: an unsigned request there meets the
    route's signature check; `/chat` and a lookalike prefix meet the middleware."""
    async with boot([], env=_env(), manifests={"e2e-assistant": ASSISTANT}) as app:
        route = await _post(app, _event(), headers={"content-type": "application/json"})
        chat = await app.client.post("/chat", json={"manifest": "e2e-assistant", "messages": []})
        lookalike = await app.client.post("/channelsx/slack/acme/events", content=b"{}")
    assert route.status_code == 401 and route.json() == {"detail": "invalid_signature"}
    assert chat.status_code == 401 and chat.json() == UNAUTHORIZED
    assert lookalike.status_code == 401 and lookalike.json() == UNAUTHORIZED


async def test_a_retried_event_starts_one_run(boot: Any, slack: FakeSlack) -> None:
    """Retried after the first run ended: an idle thread would take a second run if not claimed."""
    body = _event()
    async with boot(
        [ScriptedTurn(content="Once.")], env=_env(), manifests={"e2e-assistant": ASSISTANT}
    ) as app:
        first = await _post(app, body)
        await _drain(app)
        retry = await _post(app, body, {**signed_headers(body, SIGNING), "x-slack-retry-num": "1"})
        await _drain(app)
        runs = await _runs()
        prompts = len(app.spy.prompts)
    assert first.status_code == retry.status_code == 200
    assert (len(runs), prompts, len(slack.posts)) == (1, 1, 1)


@pytest.mark.parametrize(
    "event",
    [
        {"bot_id": "B0BOT001"},
        {"subtype": "message_changed"},
        {"type": "message", "channel_type": "channel"},
        {"text": "<@U0BOT001>"},
        {"user_team": "T0OTHER1"},
        {"source_team": "T0OTHER1"},
    ],
    ids=["bot", "edit", "channel-message", "empty", "connect-user", "connect-source"],
)
async def test_events_the_app_does_not_answer_start_nothing(
    boot: Any, slack: FakeSlack, event: dict[str, Any]
) -> None:
    async with boot([], env=_env(), manifests={"e2e-assistant": ASSISTANT}) as app:
        resp = await _post(app, _event(**event))
        runs = await _runs()
    assert resp.status_code == 200 and runs == []


async def test_a_home_workspace_member_in_a_connect_channel_is_answered(boot: Any, slack: FakeSlack) -> None:
    """The positive control for the Slack Connect cases above: `user_team` that is this workspace."""
    async with boot([], env=_env(), manifests={"e2e-assistant": ASSISTANT}) as app:
        resp = await _post(app, _event(user_team=TEAM, source_team=TEAM))
        runs = await _runs()
    assert resp.status_code == 200 and len(runs) == 1


async def test_another_workspace_starts_nothing(boot: Any, slack: FakeSlack) -> None:
    """Signed with this app's secret, but from a workspace the channel is not bound to."""
    foreign = json.loads(_event())
    foreign["team_id"] = "T0OTHER1"
    async with boot([], env=_env(), manifests={"e2e-assistant": ASSISTANT}) as app:
        resp = await _post(app, json.dumps(foreign).encode())
        runs = await _runs()
    assert resp.status_code == 200 and runs == []


@pytest.mark.parametrize("narrow", [{"users": ["U0OTHER1"]}, {"channels": ["C0OTHER1"]}])
async def test_a_sender_outside_the_allowlist_starts_nothing(
    boot: Any, slack: FakeSlack, narrow: dict[str, Any]
) -> None:
    async with boot([], env=_env(**narrow), manifests={"e2e-assistant": ASSISTANT}) as app:
        resp = await _post(app, _event())
        runs = await _runs()
    assert resp.status_code == 200 and runs == []


async def test_two_apps_in_one_workspace_keep_their_secrets_threads_and_tokens(
    boot: Any, slack: FakeSlack
) -> None:
    channels = {
        "acme": _entry(),
        "beta": _entry(signing_secret="secret:SLACK_E2E_BETA_SIGNING", bot_token="secret:SLACK_E2E_BETA_BOT"),
    }
    env = {**_env(channels), "SLACK_E2E_BETA_SIGNING": "b" * 32, "SLACK_E2E_BETA_BOT": "xoxb-beta-token"}
    body = _event()
    async with boot([ScriptedTurn(content="Beta.")], env=env, manifests={"e2e-assistant": ASSISTANT}) as app:
        crossed = await _post(app, body, signed_headers(body, SIGNING), path="beta")
        ok = await _post(app, body, signed_headers(body, "b" * 32), path="beta")
        await _drain(app)
        [run] = await _runs()
    assert crossed.status_code == 401, "acme's signature does not open beta"
    assert ok.status_code == 200 and run["thread_id"].startswith("default:channel:beta:")
    assert slack.tokens == ["Bearer xoxb-beta-token"]


@pytest.mark.parametrize("fails", [False, True], ids=["delivered", "slack-fails"])
async def test_a_second_message_while_the_first_runs_is_told_to_wait_once(
    boot: Any, slack: FakeSlack, fails: bool
) -> None:
    from felix_channels.routes import BUSY

    slack.fail = fails
    async with boot([], env=_env(), manifests={"e2e-assistant": ASSISTANT}) as app:
        await _post(app, _event("<@U0BOT001> one"))
        answers = []
        for n in (2, 3, 4):
            more = _event(
                f"<@U0BOT001> {n}",
                event_id=f"Ev000{n}",
                ts=f"1700000000.00030{n}",
                thread_ts="1700000000.000100",
            )
            answers.append(await _post(app, more))
        runs = await _runs()
    assert [(a.status_code, a.json()) for a in answers] == [(200, OK)] * 3, "a failing notice changes nothing"
    assert len(runs) == 1
    [notice] = slack.posts
    assert notice["thread_ts"] == "1700000000.000100" and notice["text"] == BUSY, "once, not per message"


async def test_a_run_that_fails_posts_a_note_not_its_error(boot: Any, slack: FakeSlack) -> None:
    script = [ScriptedTurn(error=RuntimeError("upstream exploded at /internal/path"))]
    async with boot(script, env=_env(), manifests={"e2e-assistant": ASSISTANT}) as app:
        await _post(app, _event())
        await _drain(app)
    [post] = slack.posts
    assert post["text"] == "I couldn't finish that one."
    assert "/internal/path" not in json.dumps(slack.posts)


async def test_one_sender_cannot_spend_the_whole_channels_limit(boot: Any, slack: FakeSlack) -> None:
    def mention(n: int, user: str = USER) -> bytes:
        return _event(event_id=f"Ev000{n}", ts=f"1700000000.00010{n}", channel=f"C0CHAN0{n}", user=user)

    async with boot(
        [], env=_env(rate_limit=3, user_rate_limit=2), manifests={"e2e-assistant": ASSISTANT}
    ) as app:
        mine = [await _post(app, mention(n)) for n in (1, 2, 3)]
        theirs = [await _post(app, mention(n, user="U0USER02")) for n in (4, 5)]
        runs = await _runs()
    assert [r.status_code for r in mine] == [200, 200, 429], "the sender's own limit"
    assert mine[2].json()["detail"] == "rate_limited"
    assert [r.status_code for r in theirs] == [200, 429], (
        "the channel's: three, and the refused try spent none"
    )
    assert len(runs) == 3


async def test_a_body_past_the_size_cap_is_refused(boot: Any, slack: FakeSlack) -> None:
    from felix_channels.routes import MAX_BODY_BYTES

    def padded(size: int) -> bytes:
        return _event(text="<@U0BOT001> " + "x" * (size - len(_event(text="<@U0BOT001> "))))

    at, past = padded(MAX_BODY_BYTES), padded(MAX_BODY_BYTES + 1)
    assert (len(at), len(past)) == (MAX_BODY_BYTES, MAX_BODY_BYTES + 1)
    async with boot([], env=_env(), manifests={"e2e-assistant": ASSISTANT}) as app:
        ok = await _post(app, at)
        refused = await _post(app, past)
        runs = await _runs()
    assert ok.status_code == 200
    assert refused.status_code == 413 and refused.json() == {"detail": "payload_too_large"}
    assert len(runs) == 1


async def test_a_signing_secret_that_resolves_empty_answers_unavailable_and_verifies_nothing(
    boot: Any, slack: FakeSlack
) -> None:
    body = _event()
    env = {**_env(), "SLACK_E2E_SIGNING": ""}
    headers = {
        **signed_headers(body, SIGNING),
        "x-slack-signature": slack_signature(body, "", int(time.time())),
    }
    async with boot([], env=env, manifests={"e2e-assistant": ASSISTANT}) as app:
        resp = await _post(app, body, headers)
        runs = await _runs()
    assert resp.status_code == 503 and resp.json() == {"detail": "channel_unavailable"}
    assert runs == [], "an empty key verifies nothing"


async def test_a_channel_its_manifest_refuses_says_so_once_and_is_not_retried(
    boot: Any, slack: FakeSlack
) -> None:
    """Inbound auth refusing the channel's principal refuses every sender alike -- the channel's
    configuration, so acknowledged (not retried) and said in Slack, once a minute; the same manifest
    admits the channel once it grants the scope."""
    from felix_channels.routes import UNAVAILABLE

    gated = _assistant(required_scopes=["assist:use"])
    async with boot([], env=_env(), manifests={"e2e-assistant": gated}) as app:
        refused = await _post(app, _event())
        again = await _post(app, _event(event_id="Ev0002", ts="1700000000.000300", channel="C0CHAN02"))
        refused_runs = await _runs()
    assert [p["text"] for p in slack.posts] == [UNAVAILABLE], "said once, not per message"
    assert again.status_code == 200
    async with boot([], env=_env(scopes=["assist:use"]), manifests={"e2e-assistant": gated}) as app:
        admitted = await _post(app, _event(event_id="Ev0009"))
        [run] = await _runs()
    assert refused.status_code == 200 and refused.json() == OK
    assert refused_runs == []
    assert admitted.status_code == 200 and run["state_json"]["auth"]["scopes"] == ["assist:use"]
