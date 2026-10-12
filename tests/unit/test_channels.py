"""`felix_channels`: the operator's `FELIX_CHANNELS`, Slack's signature, and which events it answers.

The flow is pinned end to end in `tests/e2e/test_slack_channel.py`; this pins every branch a
Slack request or an operator's config can reach on its own.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.support.factories import make_settings
from tests.support.optional_deps import require_optional
from tests.support.slack_fake import signed_headers

NOW = 1_800_000_000
SIGNING = "s" * 32


@pytest.fixture(autouse=True)
def _plugin() -> Any:
    return require_optional("felix_channels", "channels")


def _spec(**over: Any) -> dict[str, Any]:
    spec = {
        "platform": "slack",
        "tenant": "acme",
        "manifest": "assistant",
        "team_id": "T0TEAM01",
        "signing_secret": "secret:SLACK_SIGNING",
        "bot_token": "secret:SLACK_BOT",
    }
    spec.update(over)
    return spec


def _parse(environment: str = "production", auth_mode: str = "api_key", **over: Any) -> Any:
    from felix_channels.config import parse_channels

    settings = make_settings(
        channels=json.dumps({"c": _spec(**over)}), environment=environment, auth_mode=auth_mode
    )
    return parse_channels(settings)["c"]


@pytest.mark.parametrize(
    ("over", "says"),
    [
        ({"platform": "telegram"}, "`platform`"),
        ({"tenant": "a:b"}, "`tenant`"),
        ({"manifest": ""}, "`manifest`"),
        ({"team_id": "t-lower"}, "`team_id`"),
        ({"signing_secret": "a-literal-secret-value"}, "secret:NAME outside development"),
        ({"bot_token": ""}, "`bot_token`"),
        ({"channels": []}, "`channels`"),
        ({"users": ["u1"]}, "`users`"),
        ({"rate_limit": 0}, "`rate_limit`"),
        ({"rate_limit": True}, "`rate_limit`"),
        ({"rate_limit": 5, "user_rate_limit": 6}, "`user_rate_limit`"),
        ({"user_rate_limit": 0}, "`user_rate_limit`"),
        ({"scopes": "admin"}, "`scopes`"),
        ({"url": "x"}, "unknown keys"),
    ],
)
def test_a_malformed_channel_is_refused(over: dict[str, Any], says: str) -> None:
    with pytest.raises(ValueError) as raised:
        _parse(**over)
    assert says in str(raised.value)


def test_development_is_the_environment_and_no_auth_together() -> None:
    """The environment alone defaults to development, under Compose and in the settings."""
    with pytest.raises(ValueError, match="secret:NAME outside development"):
        _parse(environment="development", auth_mode="api_key", signing_secret=SIGNING)


async def test_a_run_is_answered_only_in_its_own_channels_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    """Core hands the hook only this plugin's runs; the hook still checks the run's tenant against
    the channel's, so a channel re-bound to another tenant since cannot carry an answer across."""
    from felix.plugins import RunFinished
    from felix_channels import delivery

    posted: list[Any] = []

    async def post(*args: Any) -> bool:
        posted.append(args[1:])
        return True

    monkeypatch.setattr(delivery, "post_message", post)
    monkeypatch.setenv("SLACK_BOT", "xoxb-test")
    settings = make_settings(channels=json.dumps({"c": _spec()}))
    origin = {"plugin": "felix-channels", "channel": "c", "conversation": "C0CHAN01", "thread_ts": ""}

    def finished(tenant: str) -> RunFinished:
        return RunFinished(
            tenant_id=tenant,
            thread_id="t",
            run_id="r1",
            status="completed",
            answer="hi",
            error="",
            origin=origin,
        )

    await delivery.on_run_finished(settings, finished("other-tenant"))
    assert posted == []
    await delivery.on_run_finished(settings, finished("acme"))
    assert posted == [("xoxb-test", "C0CHAN01", "hi", None)], "an empty thread_ts is the DM's main line"


def test_a_short_literal_is_refused_even_on_a_development_install() -> None:
    with pytest.raises(ValueError, match="at least 16 bytes"):
        _parse(environment="development", auth_mode="none", signing_secret="short")
    assert (
        _parse(environment="development", auth_mode="none", signing_secret=SIGNING).signing_secret == SIGNING
    )


def test_the_allowlists_narrow_who_may_talk_to_the_agent() -> None:
    open_ = _parse()
    narrow = _parse(channels=["C0CHAN01"], users=["U0USER01"])
    assert open_.admits("C0ANY001", "U0ANY001")
    assert narrow.admits("C0CHAN01", "U0USER01")
    assert not narrow.admits("C0CHAN02", "U0USER01")
    assert not narrow.admits("C0CHAN01", "U0USER02")


def _headers(body: bytes, *, secret: str = SIGNING, ts: int = NOW) -> dict[str, str]:
    return signed_headers(body, secret, ts=ts)


@pytest.mark.parametrize("skew", [-300, 300])
def test_slacks_signature_verifies_at_the_edge_of_its_window(skew: int) -> None:
    from felix_channels.slack import verify

    verify(SIGNING, _headers(b"{}", ts=NOW + skew), b"{}", NOW)


@pytest.mark.parametrize(
    ("headers", "secret", "code"),
    [
        ({"ts": NOW - 301}, SIGNING, "timestamp_out_of_tolerance"),
        ({"ts": NOW + 301}, SIGNING, "timestamp_out_of_tolerance"),
        ({"secret": "x" * 32}, SIGNING, "signature_mismatch"),
        ({}, "", "signing_secret_too_short"),
    ],
)
def test_slacks_signature_refuses_what_it_must(headers: dict[str, Any], secret: str, code: str) -> None:
    from felix_channels.slack import SignatureRejected, verify

    with pytest.raises(SignatureRejected, match=rf"^{code}$"):
        verify(secret, _headers(b"{}", **headers), b"{}", NOW)


def test_a_non_ascii_digit_timestamp_is_refused_not_crashed_on() -> None:
    from felix_channels.slack import SignatureRejected, verify

    with pytest.raises(SignatureRejected, match=r"^missing_or_malformed_timestamp$"):
        verify(SIGNING, {**_headers(b"{}"), "x-slack-request-timestamp": "²"}, b"{}", NOW)


def _payload(**event: Any) -> dict[str, Any]:
    base = {
        "type": "app_mention",
        "user": "U0USER01",
        "channel": "C0CHAN01",
        "ts": "1.2",
        "text": "<@U0BOT> hi",
    }
    return {"type": "event_callback", "team_id": "T0TEAM01", "event_id": "Ev0001", "event": {**base, **event}}


def test_a_mention_is_answered_in_a_thread_under_it_and_a_reply_in_its_thread() -> None:
    from felix_channels.slack import message_from

    top = message_from(_payload())
    reply = message_from(_payload(ts="1.5", thread_ts="1.2"))
    assert top is not None and (top.text, top.thread_key, top.reply_thread_ts) == ("hi", "1.2", "1.2")
    assert reply is not None and (reply.thread_key, reply.reply_thread_ts) == ("1.2", "1.2")


@pytest.mark.parametrize(
    "event",
    [
        {"bot_id": "B1"},
        {"subtype": "message_deleted"},
        {"type": "reaction_added"},
        {"type": "message", "channel_type": "channel"},
        {"text": "<@U0BOT>  "},
        {"channel": "c-lower"},
        {"ts": "1.2\n"},
        {"user": None},
    ],
)
def test_events_not_meant_for_the_agent_are_not_answered(event: dict[str, Any]) -> None:
    from felix_channels.slack import message_from

    assert message_from(_payload(**event)) is None


def test_a_run_that_did_not_complete_is_told_by_a_note_never_its_text() -> None:
    from felix_channels.delivery import reply_text

    assert reply_text("completed", "  ") == "I have no answer to that."
    assert reply_text("expired", "partial") == "That took too long, and was stopped before I could answer."
    assert reply_text("failed", "secret detail") == "I couldn't finish that one."
    assert reply_text("dead", "secret detail") == "I couldn't finish that one."


async def test_a_long_answer_is_cut_to_what_slack_accepts(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx
    from felix_channels import slack as S

    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": False, "error": "invalid_auth"})

    monkeypatch.setattr(
        S, "slack_http_client", lambda s: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    ok = await S.post_message(make_settings(), "xoxb-t", "C0CHAN01", "x" * (S.MAX_TEXT + 10), None)
    assert ok is False, "Slack's ok:false is a refusal, though it answers 200"
    assert len(sent[0]["text"]) < 40_000 and sent[0]["text"].endswith("longer than Slack shows)")
    sent.clear()
    await S.post_message(make_settings(), "xoxb-t", "C0CHAN01", "x" * (S.MAX_TEXT - 2) + "&&", None)
    assert sent[0]["text"].startswith("x" * (S.MAX_TEXT - 2) + "\n\n"), "no half `&amp;` left at the cut"
    assert "thread_ts" not in sent[0]


def test_the_entry_point_loads_the_register_function() -> None:
    import importlib.metadata as md

    import felix_channels

    [ep] = [e for e in md.entry_points(group="felix.plugins") if e.name == "channels"]
    assert ep.load() is felix_channels.register


def test_a_dm_thread_is_its_own_conversation() -> None:
    from felix_channels.slack import message_from

    dm = message_from(
        _payload(type="message", channel_type="im", channel="D0DM0001", ts="1.5", thread_ts="1.2")
    )
    main = message_from(_payload(type="message", channel_type="im", channel="D0DM0001"))
    assert dm is not None and (dm.thread_key, dm.reply_thread_ts) == ("1.2", "1.2")
    assert main is not None and (main.thread_key, main.reply_thread_ts) == ("dm", None)


def test_a_shared_channel_that_does_not_name_the_senders_workspace_is_not_answered() -> None:
    from felix_channels.slack import message_from

    shared = {**_payload(), "is_ext_shared_channel": True}
    assert message_from(shared) is None
    named = {**_payload(user_team="T0TEAM01"), "is_ext_shared_channel": True}
    assert message_from(named) is not None


async def test_secrets_are_registered_for_masking_literals_included(monkeypatch: pytest.MonkeyPatch) -> None:
    from felix import secrets as S
    from felix_channels.config import mask_secrets, resolve_secret

    monkeypatch.setattr(S, "_resolved_secret_values", [])
    literal = "a-literal-signing-secret-1234"
    await resolve_secret(make_settings(), literal)
    assert literal in S._resolved_secret_values, "a literal passes through unregistered otherwise"
    monkeypatch.setenv("SLACK_SIGNING", SIGNING)
    monkeypatch.setenv("SLACK_BOT", "xoxb-masked-at-startup")
    await mask_secrets(make_settings(channels=json.dumps({"c": _spec()})))
    assert {SIGNING, "xoxb-masked-at-startup"} <= set(S._resolved_secret_values)


@pytest.mark.parametrize("team", ["T0OTHER1", None])
def test_a_slack_connect_sender_from_another_organisation_is_not_answered(team: str | None) -> None:
    from felix_channels.slack import message_from

    outsider = _payload(user_team="T0OTHER1") if team else _payload(source_team="T0OTHER1")
    assert message_from(outsider) is None
    assert message_from(_payload(user_team="T0TEAM01", source_team="T0TEAM01")) is not None
