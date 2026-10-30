"""Connecting a published calendar feed, then reading it in a chat, over real HTTP.

Boots `create_application()` under `auth_mode=jwt`, with the connectors plugin mounting
`POST /connections/ics` as production does. The calendar service is a loopback server, reached
because `FELIX_ICS_HOSTS` names its address and the development exemption admits http -- nothing
in the plugin is pointed elsewhere by hand. The person checks in with a self-issued token, connects
a link, and a chat whose script calls `connectors__ics_events` reads it back; the link itself is
in no answer, list or prompt.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from collections.abc import AsyncIterator
from typing import Any

import pytest
from felix.auth import connections
from felix.auth.jwt import mint_token
from felix.manifests.loader import parse_manifest
from felix.manifests.store import put_version
from felix_ai.providers.scripted import ScriptedTurn
from felix_ai.types import ToolCall
from joserfc import jwk

from tests.support.icloud_fake import ics, vevent
from tests.support.loopback_http import Request, respond, serve
from tests.support.optional_deps import require_optional

_KEY = jwk.RSAKey.generate_key(2048)
SUB = "github:4242"
PERSON = f"felix-self|{SUB}"
SECRET_PATH = "/owa/calendar/abc/s3cr3t-token/calendar.ics"
#: A second link the same service publishes, for replacing one.
OTHER_PATH = "/owa/calendar/abc/s3cr3t-other/calendar.ics"
STANDUP = vevent("s1", "Standup", "20261102T163000Z", "20261102T164500Z")


@pytest.fixture(autouse=True)
def _plugin() -> Any:
    require_optional("icalendar", "ics-connectors")
    return require_optional("felix_connectors", "connectors")


class Service:
    """A calendar service on loopback: the feed at `SECRET_PATH`, or `status`/`body` instead."""

    def __init__(self) -> None:
        self.seen: list[Request] = []
        self.status = "200 OK"
        self.body = ics(STANDUP).encode()

    def __call__(self, req: Request, writer: Any) -> None:
        self.seen.append(req)
        if req.path not in (SECRET_PATH, OTHER_PATH):
            respond(writer, b"no", status="404 Not Found")
        else:
            respond(writer, self.body, status=self.status, ctype="text/calendar")


@pytest.fixture
async def service() -> AsyncIterator[tuple[Service, str]]:
    svc = Service()
    async with serve(svc) as base:
        yield svc, base + SECRET_PATH


def _env(**overrides: str) -> dict[str, str]:
    env = {
        "FELIX_ENVIRONMENT": "development",
        "FELIX_ALLOW_INSECURE": "true",
        "FELIX_AUTH_MODE": "jwt",
        "FELIX_JWT_VERIFIERS": "self:felix-self",
        "FELIX_JWKS_PUBLIC": _KEY.as_pem(private=False).decode(),
        "FELIX_JWKS_PRIVATE": _KEY.as_pem(private=True).decode(),
        "FELIX_ALLOWED_TENANTS": "acme",
        "FELIX_CONNECTION_TOKEN_KEY": base64.b64encode(os.urandom(32)).decode(),
        "FELIX_ICS_HOSTS": "127.0.0.1",
    }
    env.update(overrides)
    return env


def _bearer(settings: Any, sub: str = SUB) -> dict[str, str]:
    token = mint_token(settings, sub=sub, tenant_id="acme", scopes=[], ttl_seconds=600)
    return {"Authorization": f"Bearer {token}"}


async def _connect(app: Any, url: str, label: str = "Work", sub: str = SUB) -> Any:
    return await app.client.post(
        "/connections/ics", json={"label": label, "url": url}, headers=_bearer(app.settings, sub)
    )


def _manifest() -> Any:
    spec = {"pattern": "react", "tools": ["connectors__ics_events"], "system_prompt": {"inline": "Be brief."}}
    return parse_manifest(
        {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": "feeds"}, "spec": spec}
    )


async def test_a_person_connects_a_feed_and_their_agent_reads_it(
    boot: Any, service: tuple[Service, str], caplog: pytest.LogCaptureFixture
) -> None:
    svc, url = service
    caplog.set_level(logging.DEBUG)
    call = ToolCall(id="c1", name="connectors__ics_events", args={"start": "2026-11-02T08:00:00-07:00"})
    script = [
        ScriptedTurn(content="", tool_calls=[call], stop_reason="tool_use"),
        ScriptedTurn(content="Standup."),
    ]
    async with boot(script, env=_env()) as app:
        # The person's tenant, not the `default` one `boot(manifests=...)` writes to.
        await put_version(app.settings, "acme", "feeds", _manifest())
        made = await _connect(app, url)
        assert made.status_code == 200, made.text
        listed = await app.client.get("/connections", headers=_bearer(app.settings))
        stored = await connections.secret(app.settings, "acme", PERSON, "ics", "work")
        chat = await app.client.post(
            "/chat",
            json={"manifest": "feeds", "messages": [{"role": "user", "content": "what's on Monday?"}]},
            headers=_bearer(app.settings),
        )
        assert chat.status_code == 200, chat.text
        [_, second] = app.spy.prompts
    assert made.json() == {
        "provider": "ics",
        "account": "work",
        "label": "Work",
        "kind": "secret",
        "status": "active",
        "created_at": made.json()["created_at"],
        "updated_at": made.json()["updated_at"],
    }
    assert [(c["provider"], c["account"]) for c in listed.json()["items"]] == [("ics", "work")]
    assert stored == url
    # Fetched once to check it, once by the tool.
    assert [r.path for r in svc.seen] == [SECRET_PATH, SECRET_PATH]
    seen_by_model = " ".join(str(getattr(m, "content", "")) for m in second)
    assert "Standup · calendar Work · uid s1" in seen_by_model
    for leaked in (made.text, listed.text, chat.text, seen_by_model, caplog.text):
        assert "s3cr3t" not in leaked


async def test_connecting_a_name_again_replaces_its_link(boot: Any, service: tuple[Service, str]) -> None:
    _, url = service
    other = url.replace(SECRET_PATH, OTHER_PATH)
    async with boot([], env=_env()) as app:
        assert (await _connect(app, url)).status_code == 200
        assert (await _connect(app, url + "x")).status_code == 502  # its service refuses it: not kept
        kept = await connections.secret(app.settings, "acme", PERSON, "ics", "work")
        assert (await _connect(app, other, label=" WORK ")).status_code == 200
        replaced = await connections.secret(app.settings, "acme", PERSON, "ics", "work")
        listed = await app.client.get("/connections", headers=_bearer(app.settings))
    assert (kept, replaced) == (url, other)
    assert [c["account"] for c in listed.json()["items"]] == ["work"]


async def test_a_link_is_kept_as_it_is_fetched(boot: Any, service: tuple[Service, str]) -> None:
    _, url = service
    async with boot([], env=_env()) as app:
        assert (await _connect(app, f"  {url}  ")).status_code == 200
        kept = await connections.secret(app.settings, "acme", PERSON, "ics", "work")
    assert kept == url


async def test_a_person_keeps_a_few_feeds(boot: Any, service: tuple[Service, str]) -> None:
    from felix_connectors.ics import MAX_FEEDS

    svc, url = service
    async with boot([], env=_env()) as app:
        made = [await _connect(app, url, label=f"Feed {n}") for n in range(MAX_FEEDS)]
        fetched = len(svc.seen)
        one_more = await _connect(app, url, label="One more")
        again = await _connect(app, url, label="feed 0")
    assert [m.status_code for m in made] == [200] * MAX_FEEDS
    assert (one_more.status_code, one_more.json()["error"]) == (400, "too_many_feeds")
    assert len(svc.seen) == fetched + 1  # the refused one fetched nothing; reconnecting one did
    assert again.status_code == 200


async def test_a_link_on_a_host_not_allowed_is_never_fetched(boot: Any, service: tuple[Service, str]) -> None:
    svc, url = service
    async with boot([], env=_env(FELIX_ICS_HOSTS="calendar.example.com")) as app:
        refused = await _connect(app, url)
        listed = await app.client.get("/connections", headers=_bearer(app.settings))
    assert refused.status_code == 400
    assert refused.json() == {
        "error": "host_not_allowed",
        "message": "this deployment reads feeds only from: calendar.example.com",
    }
    assert svc.seen == [] and listed.json()["items"] == []


@pytest.mark.parametrize(
    ("status", "body", "answer", "code"),
    [
        ("200 OK", b"<html>sign in</html>", 400, "not_a_calendar"),
        ("404 Not Found", b"gone", 502, "feed_refused"),
        ("302 Found", b"", 502, "feed_refused"),
    ],
)
async def test_a_link_that_serves_no_calendar_is_not_kept(
    boot: Any, service: tuple[Service, str], status: str, body: bytes, answer: int, code: str
) -> None:
    svc, url = service
    svc.status, svc.body = status, body
    async with boot([], env=_env()) as app:
        refused = await _connect(app, url)
        listed = await app.client.get("/connections", headers=_bearer(app.settings))
    assert (refused.status_code, refused.json()["error"]) == (answer, code)
    assert "s3cr3t" not in refused.text
    assert listed.json()["items"] == []


@pytest.mark.parametrize(
    "body",
    [
        {"label": "Work"},
        {"label": "Work", "url": "https://x/y.ics", "extra": 1},
        {"label": "", "url": "https://x/y.ics"},
        "not json",
    ],
)
async def test_a_malformed_body_is_refused_without_echoing_it(
    boot: Any, service: tuple[Service, str], body: Any
) -> None:
    svc, _ = service
    async with boot([], env=_env()) as app:
        refused = await app.client.post(
            "/connections/ics",
            content=body if isinstance(body, str) else json.dumps(body),
            headers={**_bearer(app.settings), "content-type": "application/json"},
        )
    assert (refused.status_code, refused.json()) == (
        400,
        {"error": "invalid_body", "message": "send {label, url} as JSON"},
    )
    assert svc.seen == []


async def test_a_link_that_is_not_https_is_refused_unfetched(boot: Any, service: tuple[Service, str]) -> None:
    svc, url = service
    async with boot([], env=_env()) as app:
        refused = await _connect(app, url.replace("http://", "ftp://"))
    assert (refused.status_code, refused.json()["error"]) == (400, "url_invalid")
    assert svc.seen == []


async def test_a_feed_too_large_is_not_kept(
    boot: Any, service: tuple[Service, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from felix_connectors import ics as feeds_module

    monkeypatch.setattr(feeds_module, "MAX_FEED_BYTES", 100)
    _, url = service
    async with boot([], env=_env()) as app:
        refused = await _connect(app, url)
        listed = await app.client.get("/connections", headers=_bearer(app.settings))
    assert (refused.status_code, refused.json()["error"]) == (400, "feed_too_large")
    assert listed.json()["items"] == []


async def test_a_name_that_is_not_one_line_is_refused(boot: Any, service: tuple[Service, str]) -> None:
    svc, url = service
    async with boot([], env=_env()) as app:
        refused = await _connect(app, url, label="Work\x07")
    assert (refused.status_code, refused.json()["error"]) == (400, "label_invalid")
    assert svc.seen == []


async def test_a_person_may_try_a_few_times_an_hour(boot: Any, service: tuple[Service, str]) -> None:
    from felix_connectors.ics_connect import ATTEMPTS

    svc, url = service
    svc.status = "404 Not Found"
    async with boot([], env=_env()) as app:
        tries = [await _connect(app, url) for _ in range(ATTEMPTS + 1)]
        other = await _connect(app, url, sub="github:5150")
    assert [t.status_code for t in tries] == [502] * ATTEMPTS + [429]
    assert tries[-1].json()["error"] == "rate_limited"
    assert len(svc.seen) == ATTEMPTS + 1  # the refused try fetched nothing; the other person's did
    assert other.status_code == 502


async def test_without_a_vault_key_there_is_no_feed_to_connect(
    boot: Any, service: tuple[Service, str]
) -> None:
    svc, url = service
    async with boot([], env=_env(FELIX_CONNECTION_TOKEN_KEY="")) as app:
        missing = await _connect(app, url)
    assert missing.status_code == 404
    assert svc.seen == []


async def test_a_caller_naming_no_person_cannot_connect(boot: Any, service: tuple[Service, str]) -> None:
    svc, url = service
    async with boot([], env=_env()) as app:
        anonymous = await app.client.post("/connections/ics", json={"label": "Work", "url": url})
        machine = await _connect(app, url, sub="github-actions:repo:acme/widgets")
    assert anonymous.status_code == 401
    assert (machine.status_code, machine.json()["detail"]) == (403, "not_a_person")
    assert svc.seen == []


async def test_a_link_the_egress_guard_refuses_is_said_and_not_kept(
    boot: Any, service: tuple[Service, str]
) -> None:
    # https, on an allowed host, but loopback: without the development exemption the guard
    # refuses the address, and the answer says the host cannot be reached, not that the link is bad.
    svc, _ = service
    async with boot([], env=_env(FELIX_ALLOW_INSECURE="false")) as app:
        # Port 443 on loopback: nothing listens, and the guard refuses the address before connecting.
        refused = await _connect(app, "https://127.0.0.1" + SECRET_PATH)
        listed = await app.client.get("/connections", headers=_bearer(app.settings))
    assert refused.status_code == 400
    assert refused.json() == {
        "error": "host_not_allowed",
        "message": "this link's host cannot be reached from this deployment",
    }
    assert svc.seen == [] and listed.json()["items"] == []


async def test_a_deployment_that_lists_no_hosts_reads_no_feeds(
    boot: Any, service: tuple[Service, str]
) -> None:
    svc, url = service
    async with boot([], env=_env(FELIX_ICS_HOSTS="")) as app:
        refused = await _connect(app, url)
    assert refused.json() == {
        "error": "host_not_allowed",
        "message": "this deployment reads no calendar feeds",
    }
    assert svc.seen == []
