"""The `slack` destination kind: `post` publishing into a Slack conversation through a channel's app.

Driven through the kind's `validate` and `deliver`, and through the bound `post` tool, with Slack
replaced at the transport (`tests/support/slack_fake.py`) so the request is the one the plugin
builds and each answer is classified as Slack would give it.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest

from tests.support.factories import make_settings
from tests.support.optional_deps import require_optional
from tests.support.slack_fake import HANG, FakeSlack

TENANT = "acme"
CONVERSATION = "C0CHAN01"
# Short, and nothing like a real bot token's shape (gitleaks scans the history).
TOKEN = "planted-bot-token-4d2a"
# Every `ok: false` code that proves Slack posted nothing: the parametrization and the pinned list.
DEFINITE_CODES = (
    "channel_not_found",
    "not_in_channel",
    "is_archived",
    "invalid_auth",
    "account_inactive",
    "token_revoked",
    "not_authed",
    "not_allowed_token_type",
    "no_permission",
    "missing_scope",
    "restricted_action",
    "restricted_action_read_only_channel",
    "restricted_action_thread_only_channel",
    "restricted_action_non_threadable_channel",
    "team_access_not_granted",
    "msg_too_long",
    "no_text",
    "invalid_arguments",
    "ratelimited",
    "rate_limited",
)


@pytest.fixture(autouse=True)
def _plugin() -> Any:
    return require_optional("felix_channels", "channels")


@pytest.fixture
def slack(monkeypatch: pytest.MonkeyPatch) -> FakeSlack:
    from felix import destinations as D
    from felix_channels import slack as slack_mod
    from felix_channels.destination import KIND, SlackDestination

    # What `felix_channels.register` does at startup, undone after the test: whatever the process
    # had registered before (the entry point's own, after a boot) is put back.
    monkeypatch.setitem(D._kinds, KIND, SlackDestination())
    fake = FakeSlack()
    monkeypatch.setattr(slack_mod, "slack_http_client", fake.client)
    monkeypatch.setenv("SLACK_BOT", TOKEN)
    return fake


def _channel(**over: Any) -> dict[str, Any]:
    spec = {
        "platform": "slack",
        "tenant": TENANT,
        "manifest": "assistant",
        "team_id": "T0TEAM01",
        "signing_secret": "secret:SLACK_SIGNING",
        "bot_token": "secret:SLACK_BOT",
    }
    return {**spec, **over}


def _settings(dest: dict[str, Any] | None = None, channel: dict[str, Any] | None = None, **kw: Any) -> Any:
    destination = {
        "kind": "slack",
        "channel": "acme-slack",
        "conversation": CONVERSATION,
        "tenants": [TENANT],
    }
    return make_settings(
        channels=json.dumps({"acme-slack": channel or _channel()}),
        destinations=json.dumps({"team": {**destination, **(dest or {})}}),
        **kw,
    )


def _dest(settings: Any) -> Any:
    from felix.destinations import parse_destinations

    return parse_destinations(settings)["team"]


def _post(text: str = "Morning brief", title: str = "") -> Any:
    from felix.destinations import EditionKey, Post

    key = EditionKey(TENANT, "team", "brief", "brief:1")
    return Post(edition=key.edition, text=text, title=title, manifest_id="brief", key=key.post_key)


async def _deliver(settings: Any, post: Any = None) -> Any:
    from felix_channels.destination import SlackDestination

    return await SlackDestination().deliver(settings, TENANT, _dest(settings), post or _post())


# --- validate ------------------------------------------------------------------------------


def test_a_destination_through_this_tenants_slack_app_validates(slack: FakeSlack) -> None:
    from felix.destinations import resolve_destinations

    settings = _settings()
    assert list(resolve_destinations(settings, TENANT, ["team"])) == ["team"]


@pytest.mark.parametrize(
    ("dest", "says"),
    [
        ({"channel": ""}, "needs a `channel`"),
        ({"conversation": "c0chan01"}, "needs a `conversation`"),
        ({"conversation": "U0USER01"}, "needs a `conversation`"),
        ({"conversation": "C0CHAN01\n"}, "needs a `conversation`"),
        ({"conversation": None}, "needs a `conversation`"),
        ({"channel": "nowhere"}, "not registered for this tenant"),
    ],
)
def test_a_destination_the_kind_cannot_post_to_is_refused(
    slack: FakeSlack, dest: dict[str, Any], says: str
) -> None:
    from felix.destinations import DestinationError
    from felix_channels.destination import SlackDestination

    settings = _settings(dest)
    with pytest.raises(DestinationError, match=says):
        SlackDestination().validate(settings, TENANT, _dest(settings))


def test_another_tenants_slack_app_is_refused_in_the_unregistered_words(slack: FakeSlack) -> None:
    """A Slack app is bound to one tenant: a destination open to every tenant still posts through it
    only for that one, and another tenant learns nothing of it."""
    from felix.destinations import DestinationError
    from felix_channels.destination import SlackDestination

    settings = _settings({"tenants": "*"})
    SlackDestination().validate(settings, TENANT, _dest(settings))
    with pytest.raises(DestinationError) as theirs:
        SlackDestination().validate(settings, "globex", _dest(settings))
    with pytest.raises(DestinationError) as missing:
        SlackDestination().validate(
            _settings({"channel": "nowhere"}), "globex", _dest(_settings({"channel": "nowhere"}))
        )
    assert (
        str(theirs.value)
        == str(missing.value)
        == ("destination team names a Slack channel not registered for this tenant")
    )
    assert "acme" not in str(theirs.value)


async def test_an_operator_destination_posts_about_any_tenant_through_the_one_app(slack: FakeSlack) -> None:
    """The ops channel takes the harness's alerts about every tenant (`FELIX_JOB_ALERT_DESTINATION`),
    so the app's own tenant is not the question -- else every other tenant's alert is unrouted,
    retried each sweep, and never arrives."""
    from felix.destinations import resolve_destinations, resolve_harness_destination
    from felix_channels.destination import SlackDestination

    settings = _settings({"operator": True, "tenants": []})
    assert resolve_harness_destination(settings, "globex", "team").name == "team"
    delivery = await SlackDestination().deliver(settings, "globex", _dest(settings), _post())
    assert delivery.status == "posted" and len(slack.posts) == 1
    # Still the harness's alone: no manifest of any tenant may name it.
    with pytest.raises(Exception, match="unknown destination"):
        resolve_destinations(settings, TENANT, ["team"])


def test_a_malformed_channels_registry_is_a_refusal_not_a_crash(slack: FakeSlack) -> None:
    from felix.destinations import DestinationError
    from felix_channels.destination import SlackDestination

    settings = _settings(channel={"platform": "telegram"})
    with pytest.raises(DestinationError, match="not registered for this tenant"):
        SlackDestination().validate(settings, TENANT, _dest(settings))


# --- what Slack is sent --------------------------------------------------------------------


async def test_a_post_is_sent_plain_with_the_channels_token(slack: FakeSlack) -> None:
    text = "Heads up <!channel> <!here> <!everyone> <@U0123ABC> @here @everyone <https://x.example|click>"
    delivery = await _deliver(_settings(), _post(text=text))

    (sent,) = slack.posts
    assert delivery.status == "posted"
    assert slack.tokens == [f"Bearer {TOKEN}"]
    assert sent["channel"] == CONVERSATION and "thread_ts" not in sent
    assert (sent["parse"], sent["link_names"], sent["unfurl_links"], sent["unfurl_media"]) == (
        "none",
        False,
        False,
        False,
    )
    for live in ("<!channel>", "<!here>", "<!everyone>", "<@U0123ABC>", "<https://"):
        assert live not in sent["text"], live
    assert "&lt;!channel&gt;" in sent["text"] and "&lt;@U0123ABC&gt;" in sent["text"]


async def test_a_title_leads_the_text_bold_on_one_line(slack: FakeSlack) -> None:
    await _deliver(_settings(), _post(text="Body <b>", title="Morning *brief*\n<!here>"))
    assert slack.posts[0]["text"] == "*Morning brief &lt;!here&gt;*\nBody &lt;b&gt;"


@pytest.mark.parametrize("bot_token", ["secret:SLACK_BOT", TOKEN], ids=["secret-ref", "literal"])
async def test_the_token_is_masked_where_the_post_is_delivered(
    slack: FakeSlack, monkeypatch: pytest.MonkeyPatch, bot_token: str
) -> None:
    """The worker runs no plugin startup hook, so delivering is what registers the token -- a
    development install's literal too, which resolving a `secret:` ref would not register."""
    from felix.secrets import redact_text

    # A process with nothing masked yet, as a fresh worker is.
    monkeypatch.setattr("felix.secrets._resolved_secret_values", [])
    assert TOKEN in redact_text(f"leaked {TOKEN}")
    delivery = await _deliver(_settings(channel=_channel(bot_token=bot_token)))
    assert delivery.status == "posted" and slack.tokens == [f"Bearer {TOKEN}"]
    assert TOKEN not in redact_text(f"leaked {TOKEN}")


# --- what Slack's answer proves ------------------------------------------------------------


async def test_ok_true_is_posted_with_the_message_ts(slack: FakeSlack) -> None:
    delivery = await _deliver(_settings())
    assert (delivery.status, delivery.ref) == ("posted", f"{CONVERSATION}:1700000000.000200")


@pytest.mark.parametrize("code", DEFINITE_CODES)
async def test_a_definite_refusal_is_not_posted_in_fixed_words(slack: FakeSlack, code: str) -> None:
    from felix_channels.slack import DEFINITE_REFUSALS

    slack.answers = [(200, {"ok": False, "error": code, "warning": "slack's own words"})]
    delivery = await _deliver(_settings())
    assert delivery.status == "not_posted"
    assert delivery.detail == DEFINITE_REFUSALS[code]
    assert code not in delivery.detail and "slack's own words" not in delivery.detail
    assert delivery.ref is None


@pytest.mark.parametrize(
    ("code", "detail"),
    [
        (
            "channel_not_found",
            "Slack refused: the app cannot post in that conversation (missing, archived, or not joined)",
        ),
        ("token_revoked", "Slack refused the app's token"),
        ("rate_limited", "Slack is rate-limiting the app"),
    ],
)
async def test_a_family_reads_in_its_own_words(slack: FakeSlack, code: str, detail: str) -> None:
    slack.answers = [(200, {"ok": False, "error": code})]
    assert (await _deliver(_settings())).detail == detail


def test_the_definite_refusals_are_the_listed_ones() -> None:
    from felix_channels.slack import DEFINITE_REFUSALS

    assert sorted(DEFINITE_REFUSALS) == sorted(DEFINITE_CODES)


@pytest.mark.parametrize(
    "code", ["internal_error", "fatal_error", "service_unavailable", "a_code_slack_adds_later", ""]
)
async def test_any_other_slack_error_is_unknown(slack: FakeSlack, code: str) -> None:
    slack.answers = [(200, {"ok": False, "error": code})]
    delivery = await _deliver(_settings())
    assert delivery.status == "unknown"
    assert not code or code not in delivery.detail


@pytest.mark.parametrize(
    ("status", "outcome"),
    [(429, "not_posted"), (400, "not_posted"), (500, "unknown"), (503, "unknown"), (302, "unknown")],
)
async def test_a_status_other_than_200_is_classified_by_the_harness(
    slack: FakeSlack, status: int, outcome: str
) -> None:
    from felix.destinations import delivery_for_status

    slack.answers = [(status, {"ok": True, "ts": "1700000000.000200"})]
    delivery = await _deliver(_settings())
    assert delivery == delivery_for_status(status, ref=None)
    assert delivery.status == outcome, "a body saying ok does not outweigh the status"


async def test_a_slack_that_takes_the_request_and_never_answers_is_unknown(
    slack: FakeSlack, monkeypatch: pytest.MonkeyPatch
) -> None:
    from felix_channels import slack as slack_mod

    monkeypatch.setattr(slack_mod, "CLASSIFIED_POST_TIMEOUT_S", 0.2)
    slack.answers = [HANG]
    delivery = await _deliver(_settings())
    assert len(slack.posts) == 1, "the request reached Slack"
    assert delivery.status == "unknown"


async def test_a_refused_connection_is_not_posted(slack: FakeSlack) -> None:
    slack.answers = [httpx.ConnectError("connection refused")]
    delivery = await _deliver(_settings())
    assert slack.posts == []
    assert (delivery.status, delivery.detail) == ("not_posted", "the destination could not be reached")


async def test_a_channel_removed_since_the_compile_is_not_posted(slack: FakeSlack) -> None:
    from felix_channels.destination import SlackDestination

    compiled = _settings()
    now = _settings(channel=_channel(tenant="globex"))
    delivery = await SlackDestination().deliver(now, TENANT, _dest(compiled), _post())
    assert delivery.status == "not_posted"
    assert slack.posts == []


# --- through the tool ----------------------------------------------------------------------


async def test_the_token_never_reaches_a_tool_result_or_a_log_line(
    slack: FakeSlack, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In a fresh worker -- nothing masked yet, no plugin startup hook -- the first post that quotes
    the bot token still sends it to no one: the kind's `secrets` are registered before the text is
    cleaned."""
    from felix.context import AuthContext, RequestContext, async_run_with_context
    from felix.destinations import resolve_destinations
    from felix.destinations.tool import make_post_tool
    from felix.manifests.loader import parse_manifest
    from felix.tools.types import ToolInvocationCtx, tool_output_content

    monkeypatch.setattr("felix.secrets._resolved_secret_values", [])
    settings = _settings()
    manifest = parse_manifest(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": "brief"},
            "spec": {"pattern": "react", "destinations": {"ids": ["team"]}},
        }
    )
    tool = make_post_tool(
        settings,
        tenant_id=TENANT,
        manifest=manifest,
        destinations=resolve_destinations(settings, TENANT, ["team"]),
    )
    auth = AuthContext(
        principal_sub="cron", tenant_id=TENANT, anonymous=False, scheme="api_key", scheduled=True
    )
    req = RequestContext(settings=settings, auth=auth, manifest_id="brief", thread_id=f"{TENANT}:job")
    slack.answers = [(200, {"ok": False, "error": "invalid_auth"}), (500, {"ok": False})]
    outs = []
    with caplog.at_level(logging.DEBUG):
        async with async_run_with_context(req):
            for edition in ("e1", "e2"):
                args = {"destination": "team", "edition": edition, "text": f"say {TOKEN}"}
                out = await tool.executor.execute(args, ToolInvocationCtx(thread_id="t", tool_call_id="c1"))
                outs.append(tool_output_content(out))

    assert json.loads(outs[0])["error"] == "not_posted" and json.loads(outs[1])["error"] == "maybe_posted"
    assert all(TOKEN not in o for o in outs)
    assert TOKEN not in caplog.text
    assert all(TOKEN not in p["text"] for p in slack.posts), "the post's own text is redacted too"


def test_the_plugin_registers_the_slack_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    import felix_channels
    from felix import destinations as D
    from felix.plugins import PluginRegistry
    from felix_channels.destination import SlackDestination

    monkeypatch.delitem(D._kinds, "slack", raising=False)
    felix_channels.register(PluginRegistry())
    assert isinstance(D.destination_kind("slack"), SlackDestination)


# --- the second line of defence, and what is refused before anything is sent ------------------


async def test_deliver_redacts_the_token_from_the_text_itself(
    slack: FakeSlack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whatever cleaned the post, `deliver` redacts the token it sends with from the text it sends."""
    monkeypatch.setattr("felix.secrets._resolved_secret_values", [])
    await _deliver(_settings(), _post(text=f"the token is {TOKEN}", title=f"t {TOKEN}"))
    (sent,) = slack.posts
    assert TOKEN not in sent["text"]


async def test_the_kind_declares_its_token_for_masking_before_the_text_is_cleaned(slack: FakeSlack) -> None:
    from felix.destinations import destination_secrets
    from felix_channels.destination import SlackDestination

    settings = _settings()
    assert await destination_secrets(SlackDestination(), settings, TENANT, _dest(settings)) == [TOKEN]
    assert await destination_secrets(SlackDestination(), settings, "globex", _dest(settings)) == []


async def test_a_token_that_does_not_resolve_is_not_posted_and_sends_nothing(
    slack: FakeSlack, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SLACK_BOT")
    delivery = await _deliver(_settings())
    assert (delivery.status, delivery.detail) == ("not_posted", "the Slack app's token could not be resolved")
    assert slack.posts == []


async def test_a_conversation_that_fails_its_grammar_at_delivery_sends_nothing(slack: FakeSlack) -> None:
    from felix_channels.destination import SlackDestination

    settings = _settings({"conversation": "not-a-slack-id"})
    delivery = await SlackDestination().deliver(settings, TENANT, _dest(settings), _post())
    assert delivery.status == "not_posted"
    assert slack.posts == []


async def test_a_stalled_dial_is_not_posted_and_the_deadline_leaves_it_room(slack: FakeSlack) -> None:
    import httpx
    from felix import destinations as D
    from felix_channels import slack as slack_mod

    slack.answers = [httpx.ConnectTimeout("dial stalled")]
    delivery = await _deliver(_settings())
    assert (delivery.status, delivery.detail) == ("not_posted", "the destination could not be reached")
    assert slack.posts == []
    (timeout,) = slack.timeouts
    assert timeout.connect == slack_mod.POST_CONNECT_TIMEOUT_S
    # The dial's own timeout fires first, inside the call's deadline, inside the harness's budget.
    assert slack_mod.POST_CONNECT_TIMEOUT_S < slack_mod.CLASSIFIED_POST_TIMEOUT_S < D.DELIVERY_BUDGET_S


async def test_the_reply_path_keeps_its_own_bound(slack: FakeSlack) -> None:
    from felix_channels.slack import post_message

    assert await post_message(make_settings(), TOKEN, CONVERSATION, "hi", None)
    assert slack.timeouts == [None], "the reply path builds its default client"


@pytest.mark.parametrize(
    "body", [[1, 2], "ok", b"<html>bad gateway</html>", b""], ids=["list", "string", "html", "empty"]
)
async def test_a_200_whose_body_is_not_an_answer_is_unknown(slack: FakeSlack, body: Any) -> None:
    slack.answers = [(200, body)]
    delivery = await _deliver(_settings())
    assert delivery.status == "unknown"
    assert len(slack.posts) == 1


async def test_slacks_code_and_a_send_errors_type_reach_the_log_and_the_token_does_not(
    slack: FakeSlack, caplog: pytest.LogCaptureFixture
) -> None:
    slack.answers = [(200, {"ok": False, "error": "not_in_channel"}), httpx.ReadTimeout("no answer")]
    with caplog.at_level(logging.WARNING, logger="felix_channels"):
        await _deliver(_settings())
        await _deliver(_settings())
    assert "slack_post_refused error=not_in_channel" in caplog.text
    assert "slack_post_failed exc=ReadTimeout outcome=unknown" in caplog.text
    assert TOKEN not in caplog.text
