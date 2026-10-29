"""Connecting a Google account to a person's vault, over real HTTP, through the real callback.

Boots `create_application()` under `auth_mode=jwt` with Google faked at `google_http_client`. A
person signs in with Google (the sign-in's own round trip), then connects in three steps: `POST
/connections/google/start` as themselves, the browser to Google and back through
`/connections/google/callback` carrying only the cookie the start set, and `POST
/connections/google/finish` as themselves to keep what came back. `GET /connections` shows it.
What the vault then holds is driven through the provider it registered: a refresh from Google, a
refusal, an outage, a removal that withdraws the grant. Nothing here writes the vault by hand --
the consent flow is the only writer production has.

The fake keeps grants per Google account the way Google does: scopes accumulate under
`include_granted_scopes`, a refresh token comes with a consent screen or a first grant, and
revoking any token ends the account's whole grant.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from felix.auth import connections, google
from felix.auth.google import GOOGLE_AUTHORIZE_URL
from joserfc import jwk

from tests.support.google_fake import CLIENT_ID, CLIENT_SECRET, KID_SUB, USERINFO_EMAIL, FakeGoogle

_KEY = jwk.RSAKey.generate_key(2048)
APP = "http://localhost:5181"
SECURE_APP = "https://app.example"
ACCOUNTS = {
    "parent@example.com": {"tenant": "family", "scopes": []},
    "other@example.com": {"tenant": "family", "scopes": []},
}
PARENT_SUB = "1000000000000000002"
OTHER_SUB = "1000000000000000003"
PERSON = f"felix-self|google:{PARENT_SUB}"
SECOND_SUB = "1000000000000000009"
CALENDAR = "https://www.googleapis.com/auth/calendar.readonly"
COMPOSE = "https://www.googleapis.com/auth/gmail.compose"
FLOW, HANDOFF = "felix_google_connect_flow", "felix_google_connect_handoff"


def _env(**overrides: str) -> dict[str, str]:
    env = {
        "FELIX_ENVIRONMENT": "development",
        "FELIX_AUTH_MODE": "jwt",
        "FELIX_JWT_VERIFIERS": "self:felix-self",
        "FELIX_JWKS_PUBLIC": _KEY.as_pem(private=False).decode(),
        "FELIX_JWKS_PRIVATE": _KEY.as_pem(private=True).decode(),
        "FELIX_ALLOWED_TENANTS": "family",
        "FELIX_GOOGLE_CLIENT_ID": CLIENT_ID,
        "FELIX_GOOGLE_CLIENT_SECRET": CLIENT_SECRET,
        "FELIX_GOOGLE_COOKIE_KEY": base64.b64encode(os.urandom(32)).decode(),
        "FELIX_GOOGLE_REDIRECT_ORIGINS": APP,
        "FELIX_GOOGLE_ACCOUNTS": json.dumps(ACCOUNTS),
        "FELIX_CONNECTION_TOKEN_KEY": base64.b64encode(os.urandom(32)).decode(),
    }
    env.update(overrides)
    return env


@pytest.fixture
def fake_google(monkeypatch: pytest.MonkeyPatch) -> FakeGoogle:
    fake = FakeGoogle()
    # Patched by name in each module that imported it; a third importer would bypass the fake.
    from felix.auth import google_connect

    monkeypatch.setattr(google, "google_http_client", lambda settings: fake.client())
    monkeypatch.setattr(google_connect, "google_http_client", lambda settings: fake.client())
    monkeypatch.setattr(google, "_keys", google.new_key_cache())
    return fake


def _query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


async def _signed_in(
    client: Any, fake: FakeGoogle, sub: str = PARENT_SUB, email: str = "parent@example.com"
) -> dict[str, str]:
    """A Google sign-in; returns the bearer header to act as that person."""
    started = await client.get("/auth/google/authorize", params={"return_to": f"{APP}/"})
    query = _query(started.headers["location"])
    code = fake.issue_code(query, sub=sub, email=email)
    await client.get("/auth/google/callback", params={"code": code, "state": query["state"]})
    token = (await client.post("/auth/google/exchange")).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


async def _start(
    client: Any, auth: dict[str, str], scopes: list[str] | None = None, return_to: str = f"{APP}/settings"
) -> dict[str, str]:
    started = await client.post(
        "/connections/google/start",
        json={"return_to": return_to, "scopes": scopes or ["calendar_read", "gmail_compose"]},
        headers=auth,
    )
    assert started.status_code == 200, started.text
    url = started.json()["authorize_url"]
    assert url.startswith(GOOGLE_AUTHORIZE_URL + "?")
    return _query(url)


async def _callback(client: Any, query: dict[str, str], code: str = "", **params: str) -> str:
    # Where Google sends the browser: the redirect URI the start asked for, less chat-ui's `/api`.
    path = urlsplit(query["redirect_uri"]).path.removeprefix("/api")
    back = await client.get(path, params={"code": code, "state": query["state"], **params})
    assert back.status_code == 302, back.text
    location = back.headers["location"]
    assert location.startswith(f"{APP}/settings#"), location
    return location.split("#", 1)[1]


async def _connect(client: Any, fake: FakeGoogle, auth: dict[str, str], **issue: Any) -> Any:
    """Start, approve at Google, come back, keep. Returns the finish response."""
    scopes = issue.pop("scopes", None)
    query = await _start(client, auth, scopes)
    issue.setdefault("sub", KID_SUB)
    issue.setdefault("email", "family@example.com")
    assert await _callback(client, query, fake.issue_code(query, **issue)) == "felix_connect=pending"
    return await client.post("/connections/google/finish", headers=auth)


# --- connecting ---------------------------------------------------------------------------


async def test_a_person_connects_google_and_sees_it_as_theirs(boot: Any, fake_google: FakeGoogle) -> None:
    async with boot([], env=_env()) as app:
        auth = await _signed_in(app.client, fake_google)
        query = await _start(app.client, auth)
        code = fake_google.issue_code(query, sub=KID_SUB, email="family@example.com")
        assert await _callback(app.client, query, code) == "felix_connect=pending"
        # Nothing is stored until the person asks to keep it.
        assert await connections.list_connections(app.settings, "family") == []
        kept = await app.client.post("/connections/google/finish", headers=auth)
        listed = (await app.client.get("/connections", headers=auth)).json()
    assert query["access_type"] == "offline"
    assert "consent" in query["prompt"].split()
    assert query["include_granted_scopes"] == "true"
    assert query["code_challenge_method"] == "S256"
    assert query["scope"].split() == ["openid", "email", CALENDAR, COMPOSE]
    assert query["redirect_uri"] == f"{APP}/api/connections/google/callback"
    assert kept.status_code == 200, kept.text
    assert [(c["provider"], c["account"], c["label"], c["kind"], c["status"]) for c in listed["items"]] == [
        ("google", KID_SUB, "family@example.com", "oauth", "active")
    ]
    assert kept.json()["account"] == KID_SUB
    assert set(listed["items"][0]["scopes"]) == {"openid", USERINFO_EMAIL, CALENDAR, COMPOSE}
    assert fake_google.refresh_tokens and fake_google.access_tokens
    assert not any(t in json.dumps(listed) for t in [*fake_google.refresh_tokens, *fake_google.access_tokens])


async def test_connecting_more_later_keeps_what_was_granted_before(
    boot: Any, fake_google: FakeGoogle
) -> None:
    async with boot([], env=_env()) as app:
        auth = await _signed_in(app.client, fake_google)
        assert (await _connect(app.client, fake_google, auth, scopes=["calendar_read"])).status_code == 200
        assert (await _connect(app.client, fake_google, auth, scopes=["gmail_compose"])).status_code == 200
        (row,) = (await app.client.get("/connections", headers=auth)).json()["items"]
    assert {CALENDAR, COMPOSE} <= set(row["scopes"])


async def test_a_partial_grant_is_kept_as_granted(boot: Any, fake_google: FakeGoogle) -> None:
    # Granular consent: the person unticks the mail scope. What is kept says what the token can do.
    async with boot([], env=_env()) as app:
        auth = await _signed_in(app.client, fake_google)
        await _connect(app.client, fake_google, auth, grant_only=f"openid email {CALENDAR}")
        (row,) = (await app.client.get("/connections", headers=auth)).json()["items"]
    assert COMPOSE not in row["scopes"] and CALENDAR in row["scopes"]


async def test_the_grant_is_the_starters_and_refreshes_through_google(
    boot: Any, fake_google: FakeGoogle
) -> None:
    async with boot([], env=_env()) as app:
        auth = await _signed_in(app.client, fake_google)
        await _connect(app.client, fake_google, auth)
        key = ("family", PERSON, "google", KID_SUB)
        assert (
            await connections.access_token(app.settings, "family", PERSON, "google")
            == fake_google.access_tokens[-1]
        )
        assert fake_google.refreshes == 0
        await connections._force_expiry_for_tests(app.settings, key)
        assert await connections.access_token(app.settings, "family", PERSON, "google") == "ya29.refreshed-1"
        # Google keeps its refresh token on a refresh, so the stored one still works.
        await connections._force_expiry_for_tests(app.settings, key)
        assert await connections.access_token(app.settings, "family", PERSON, "google") == "ya29.refreshed-2"


async def test_a_worker_that_never_imported_google_still_refreshes_and_withdraws_through_it(
    boot: Any, fake_google: FakeGoogle
) -> None:
    async with boot([], env=_env()) as app:
        auth = await _signed_in(app.client, fake_google)
        await _connect(app.client, fake_google, auth)
        key = ("family", PERSON, "google", KID_SUB)
        (refresh,) = fake_google.refresh_tokens
        connections.unregister_connection_provider("google")
        await connections._force_expiry_for_tests(app.settings, key)
        assert await connections.access_token(app.settings, "family", PERSON, "google") == "ya29.refreshed-1"
        connections.unregister_connection_provider("google")
        assert await connections.remove_connection(app.settings, *key) is True
    assert fake_google.revoked == [refresh]


async def test_reconnecting_a_revoked_account_makes_it_work_again(boot: Any, fake_google: FakeGoogle) -> None:
    async with boot([], env=_env()) as app:
        auth = await _signed_in(app.client, fake_google)
        await _connect(app.client, fake_google, auth)
        key = ("family", PERSON, "google", KID_SUB)
        first = await connections.get_connection(app.settings, *key)
        fake_google.grants.clear()  # revoked at myaccount.google.com
        await connections._force_expiry_for_tests(app.settings, key)
        with pytest.raises(connections.ConnectionRevoked):
            await connections.access_token(app.settings, "family", PERSON, "google")
        await asyncio.sleep(0.002)
        await _connect(app.client, fake_google, auth)
        (row,) = (await app.client.get("/connections", headers=auth)).json()["items"]
        token = await connections.access_token(app.settings, "family", PERSON, "google")
    assert row["status"] == "active"
    assert row["created_at"] == first["created_at"] and row["updated_at"] > first["updated_at"]
    assert token == fake_google.access_tokens[-1]


async def test_two_google_accounts_are_two_connections(boot: Any, fake_google: FakeGoogle) -> None:
    async with boot([], env=_env()) as app:
        auth = await _signed_in(app.client, fake_google)
        await _connect(app.client, fake_google, auth)
        await _connect(app.client, fake_google, auth, sub=SECOND_SUB, email="work@example.com")
        listed = (await app.client.get("/connections", headers=auth)).json()["items"]
        with pytest.raises(connections.AmbiguousConnection):
            await connections.access_token(app.settings, "family", PERSON, "google")
        first, second = fake_google.access_tokens[-2:]
        assert await connections.access_token(app.settings, "family", PERSON, "google", KID_SUB) == first
        assert await connections.access_token(app.settings, "family", PERSON, "google", SECOND_SUB) == second
        removed = await app.client.delete(f"/connections/google/{KID_SUB}", headers=auth)
        await connections._force_expiry_for_tests(app.settings, ("family", PERSON, "google", SECOND_SUB))
        still = await connections.access_token(app.settings, "family", PERSON, "google")
    assert [c["account"] for c in listed] == sorted([KID_SUB, SECOND_SUB])
    assert removed.status_code == 204
    assert len(fake_google.revoked) == 1
    assert still.startswith("ya29.refreshed-")


# --- what Google answers ------------------------------------------------------------------


async def test_a_grant_google_revoked_is_marked_revoked_on_its_next_use(
    boot: Any, fake_google: FakeGoogle
) -> None:
    async with boot([], env=_env()) as app:
        auth = await _signed_in(app.client, fake_google)
        await _connect(app.client, fake_google, auth)
        fake_google.grants.clear()
        await connections._force_expiry_for_tests(app.settings, ("family", PERSON, "google", KID_SUB))
        with pytest.raises(connections.ConnectionRevoked):
            await connections.access_token(app.settings, "family", PERSON, "google")
        (row,) = (await app.client.get("/connections", headers=auth)).json()["items"]
    assert (row["account"], row["status"]) == (KID_SUB, "revoked")


@pytest.mark.parametrize(
    "failure",
    [
        (503, {"error": "backend_error"}),
        (401, {"error": "invalid_client"}),
        (502, "<html>bad gateway</html>"),
    ],
)
async def test_an_outage_at_google_revokes_nothing(boot: Any, fake_google: FakeGoogle, failure: Any) -> None:
    async with boot([], env=_env()) as app:
        auth = await _signed_in(app.client, fake_google)
        await _connect(app.client, fake_google, auth)
        fake_google.fail_refresh = failure
        await connections._force_expiry_for_tests(app.settings, ("family", PERSON, "google", KID_SUB))
        with pytest.raises(connections.ProviderFailed):
            await connections.access_token(app.settings, "family", PERSON, "google")
        (row,) = (await app.client.get("/connections", headers=auth)).json()["items"]
    assert row["status"] == "active"


@pytest.mark.parametrize(
    ("issue", "reason"),
    [
        ({"query_nonce": "another-flow"}, "invalid_id_token"),  # an ID token from another flow
        ({"email_verified": False}, "invalid_id_token"),
        ({"email_verified": "true"}, "invalid_id_token"),
        ({"aud": "other.apps.googleusercontent.com"}, "invalid_id_token"),
    ],
)
async def test_an_id_token_that_does_not_verify_stores_nothing(
    boot: Any, fake_google: FakeGoogle, issue: dict[str, Any], reason: str
) -> None:
    async with boot([], env=_env()) as app:
        auth = await _signed_in(app.client, fake_google)
        query = await _start(app.client, auth)
        nonce = issue.pop("query_nonce", None)
        code = fake_google.issue_code({**query, "nonce": nonce} if nonce else query, **issue)
        assert await _callback(app.client, query, code) == f"felix_connect_error={reason}"
        finished = await app.client.post("/connections/google/finish", headers=auth)
        assert await connections.list_connections(app.settings, "family") == []
    assert finished.status_code == 400


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        ((503, {"error": "backend_error"}), "google_unavailable"),
        ((502, "<html>bad gateway</html>"), "google_unavailable"),
        ((400, {"error": "invalid_grant"}), "invalid_grant"),
        ((400, {"error": "redirect_uri_mismatch"}), "google_config_error"),
    ],
)
async def test_an_exchange_google_refuses_comes_back_as_its_reason(
    boot: Any, fake_google: FakeGoogle, failure: Any, reason: str
) -> None:
    async with boot([], env=_env()) as app:
        auth = await _signed_in(app.client, fake_google)
        query = await _start(app.client, auth)
        code = fake_google.issue_code(query)
        fake_google.fail_token = failure
        assert await _callback(app.client, query, code) == f"felix_connect_error={reason}"


async def test_a_code_google_never_issued_is_refused(boot: Any, fake_google: FakeGoogle) -> None:
    async with boot([], env=_env()) as app:
        auth = await _signed_in(app.client, fake_google)
        query = await _start(app.client, auth)
        assert await _callback(app.client, query, "never-issued") == "felix_connect_error=invalid_grant"


async def test_connecting_an_account_again_brings_a_refresh_token_again(
    boot: Any, fake_google: FakeGoogle
) -> None:
    # Google sends a refresh token on a consent screen or an account's first grant; the start asks
    # for the screen every time, so a second connect of the same account is kept too.
    async with boot([], env=_env()) as app:
        auth = await _signed_in(app.client, fake_google)
        assert (await _connect(app.client, fake_google, auth)).status_code == 200
        query = await _start(app.client, auth)
        code = fake_google.issue_code(query, sub=KID_SUB)
        assert await _callback(app.client, query, code) == "felix_connect=pending"
        # Without the screen Google sends none, and nothing is kept.
        query = await _start(app.client, auth)
        code = fake_google.issue_code({**query, "prompt": "select_account"}, sub=KID_SUB)
        assert await _callback(app.client, query, code) == "felix_connect_error=no_refresh_token"


async def test_cancelling_on_googles_screen_comes_back_as_such(boot: Any, fake_google: FakeGoogle) -> None:
    async with boot([], env=_env()) as app:
        auth = await _signed_in(app.client, fake_google)
        query = await _start(app.client, auth)
        before = fake_google.token_calls
        cancelled = await _callback(app.client, query, error="access_denied")
        query = await _start(app.client, auth)
        refused = await _callback(app.client, query, error="admin_policy_enforced")
    assert cancelled == "felix_connect_error=access_denied"
    assert refused == "felix_connect_error=google_config_error"
    assert fake_google.token_calls == before


async def test_a_failed_withdrawal_at_google_still_forgets_the_row_and_says_so(
    boot: Any, fake_google: FakeGoogle, caplog: pytest.LogCaptureFixture
) -> None:
    async with boot([], env=_env()) as app:
        auth = await _signed_in(app.client, fake_google)
        await _connect(app.client, fake_google, auth)
        fake_google.fail_revoke = (503, {"error": "backend_error"})
        removed = await app.client.delete(f"/connections/google/{KID_SUB}", headers=auth)
        listed = (await app.client.get("/connections", headers=auth)).json()
    assert removed.status_code == 204
    assert listed == {"items": []}
    # The grant may still be live at Google; the operator hears of it.
    assert "connection_revoke_failed provider=google error=RuntimeError" in caplog.text


async def test_disconnecting_withdraws_the_grant_at_google(boot: Any, fake_google: FakeGoogle) -> None:
    async with boot([], env=_env()) as app:
        auth = await _signed_in(app.client, fake_google)
        await _connect(app.client, fake_google, auth)
        (refresh,) = fake_google.refresh_tokens
        removed = await app.client.delete(f"/connections/google/{KID_SUB}", headers=auth)
        listed = (await app.client.get("/connections", headers=auth)).json()
    assert removed.status_code == 204
    assert fake_google.revoked == [refresh]
    assert listed == {"items": []}


# --- whose browser, whose vault -----------------------------------------------------------


async def test_a_flow_planted_in_someone_elses_browser_puts_their_grant_in_nobodys_vault(
    boot: Any, fake_google: FakeGoogle
) -> None:
    # A starts a flow and plants its cookie in V's browser; V approves their own account and
    # comes back. V's app finishes as V: the grant was started by A, so nobody keeps it.
    async with boot([], env=_env()) as app:
        attacker = await _signed_in(app.client, fake_google, OTHER_SUB, "other@example.com")
        query = await _start(app.client, attacker)
        victim = await _signed_in(app.client, fake_google)
        code = fake_google.issue_code(query, sub=SECOND_SUB, email="victim@example.com")
        assert await _callback(app.client, query, code) == "felix_connect=pending"
        finished = await app.client.post("/connections/google/finish", headers=victim)
        stored = await connections.list_connections(app.settings, "family")
        replay = await app.client.post("/connections/google/finish", headers=attacker)
    assert (finished.status_code, finished.json()["error"]) == (403, "not_your_connection")
    assert stored == []
    # Dropped, not revoked: a revoke would end the account's whole grant, any real connection too.
    assert fake_google.revoked == []
    assert replay.status_code == 400


async def test_a_consent_link_finished_in_another_browser_stores_nothing(
    boot: Any, fake_google: FakeGoogle
) -> None:
    async with boot([], env=_env()) as app:
        auth = await _signed_in(app.client, fake_google)
        query = await _start(app.client, auth)
        code = fake_google.issue_code(query, sub=KID_SUB, email="victim@example.com")
        app.client.cookies.clear()
        before = fake_google.token_calls
        back = await app.client.get(
            "/connections/google/callback", params={"code": code, "state": query["state"]}
        )
        assert (back.status_code, back.json()["error"]) == (400, "connect_expired")
        assert await connections.list_connections(app.settings, "family") == []
    assert code in fake_google.codes, "the code was exchanged"
    assert fake_google.token_calls == before


async def test_a_state_this_browser_did_not_start_is_refused_unexchanged(
    boot: Any, fake_google: FakeGoogle
) -> None:
    async with boot([], env=_env()) as app:
        auth = await _signed_in(app.client, fake_google)
        query = await _start(app.client, auth)
        code = fake_google.issue_code(query)
        assert (
            await _callback(app.client, {**query, "state": "forged"}, code)
            == "felix_connect_error=state_mismatch"
        )
        assert FLOW not in app.client.cookies
    assert code in fake_google.codes, "the code was exchanged"


@pytest.mark.parametrize("damage", ["expired", "tampered", "sign-in"])
async def test_a_flow_cookie_that_is_not_this_flows_is_refused(
    boot: Any, fake_google: FakeGoogle, damage: str
) -> None:
    from felix_api.routes import connections as routes

    async with boot([], env=_env()) as app:
        auth = await _signed_in(app.client, fake_google)
        query = await _start(app.client, auth)
        value = app.client.cookies[FLOW]
        if damage == "expired":
            flow = routes._google_unseal(app.settings, routes._GOOGLE_FLOW_PURPOSE, value)
            value = routes._google_seal(
                app.settings, routes._GOOGLE_FLOW_PURPOSE, {**flow, "e": int(time.time()) - 1}
            )
        elif damage == "tampered":
            value = value[:-3] + ("A" if value[-3] != "A" else "B") + value[-2:]
        else:
            # Sign-in's own flow cookie, sealed for another purpose, under this flow's name.
            await app.client.get("/auth/google/authorize", params={"return_to": f"{APP}/"})
            value = app.client.cookies["felix_google_flow"]
        app.client.cookies.set(FLOW, value)
        back = await app.client.get(
            "/connections/google/callback", params={"code": "x", "state": query["state"]}
        )
    assert (back.status_code, back.json()["error"]) == (400, "connect_expired")


async def test_the_grant_waits_for_one_finish_only(boot: Any, fake_google: FakeGoogle) -> None:
    async with boot([], env=_env()) as app:
        auth = await _signed_in(app.client, fake_google)
        assert (await _connect(app.client, fake_google, auth)).status_code == 200
        assert FLOW not in app.client.cookies and HANDOFF not in app.client.cookies
        again = await app.client.post("/connections/google/finish", headers=auth)
    assert (again.status_code, again.json()["error"]) == (400, "connect_expired")


@pytest.mark.parametrize(
    ("origins", "cookies", "read"),
    [
        # Every origin https: only the `__Host-` name, which nothing but this host can set.
        (SECURE_APP, {FLOW: "planted"}, ""),
        (SECURE_APP, {f"__Host-{FLOW}": "ours", FLOW: "planted"}, "ours"),
        # An http origin (dev) cannot carry a `__Host-` cookie, so the bare name is read there.
        (f"{APP},{SECURE_APP}", {FLOW: "dev"}, "dev"),
    ],
)
def test_over_https_a_bare_cookie_name_is_not_read(origins: str, cookies: dict[str, str], read: str) -> None:
    from types import SimpleNamespace

    from felix_api.routes import connections as routes

    from tests.support.factories import make_settings

    settings = make_settings(google_redirect_origins=origins)
    assert routes._read_cookie(SimpleNamespace(cookies=cookies), settings, routes._GOOGLE_FLOW) == read


async def test_over_https_the_cookies_are_host_only_secure_and_httponly(
    boot: Any, fake_google: FakeGoogle
) -> None:
    async with boot([], env=_env(FELIX_GOOGLE_REDIRECT_ORIGINS=f"{APP},{SECURE_APP}")) as app:
        auth = await _signed_in(app.client, fake_google)
        started = await app.client.post(
            "/connections/google/start",
            json={"return_to": f"{SECURE_APP}/settings", "scopes": ["calendar_read"]},
            headers=auth,
        )
        plain = await app.client.post(
            "/connections/google/start",
            json={"return_to": f"{APP}/settings", "scopes": ["calendar_read"]},
            headers=auth,
        )
    secure = SimpleCookie(started.headers["set-cookie"])[f"__Host-{FLOW}"]
    assert (
        secure["secure"],
        secure["httponly"],
        secure["samesite"].lower(),
        secure["path"],
        secure["max-age"],
    ) == (
        True,
        True,
        "lax",
        "/",
        "600",
    )
    assert not SimpleCookie(plain.headers["set-cookie"])[FLOW]["secure"]


# --- who may, and when --------------------------------------------------------------------


async def test_starting_needs_a_person_and_an_offered_scope(boot: Any, fake_google: FakeGoogle) -> None:
    async with boot([], env=_env()) as app:
        nobody = await app.client.post(
            "/connections/google/start", json={"return_to": f"{APP}/", "scopes": ["calendar_read"]}
        )
        auth = await _signed_in(app.client, fake_google)
        unknown = await app.client.post(
            "/connections/google/start", json={"return_to": f"{APP}/", "scopes": ["contacts"]}, headers=auth
        )
        elsewhere = await app.client.post(
            "/connections/google/start",
            json={"return_to": "https://evil.example.com/", "scopes": ["calendar_read"]},
            headers=auth,
        )
        unfinished = await app.client.post("/connections/google/finish")
    assert nobody.status_code == 401
    assert (unknown.status_code, unknown.json()["detail"]) == (422, "unknown_scope")
    assert elsewhere.status_code == 400
    assert unfinished.status_code == 401


async def test_connecting_is_closed_without_a_vault_key(boot: Any, fake_google: FakeGoogle) -> None:
    async with boot([], env=_env(FELIX_CONNECTION_TOKEN_KEY="")) as app:
        auth = await _signed_in(app.client, fake_google)
        started = await app.client.post(
            "/connections/google/start",
            json={"return_to": f"{APP}/", "scopes": ["calendar_read"]},
            headers=auth,
        )
        # Not public either: the middleware wants a credential before the route can 404.
        callback = await app.client.get("/connections/google/callback", params={"state": "x", "code": "y"})
    assert started.status_code == 404
    assert callback.status_code == 401


async def test_a_store_that_fails_answers_in_its_own_words_and_clears_the_handoff(
    boot: Any, fake_google: FakeGoogle, monkeypatch: pytest.MonkeyPatch
) -> None:
    from felix.auth import google_connect

    async def broken(*args: Any) -> Any:
        raise RuntimeError("postgres at db-7.internal refused")

    async with boot([], env=_env()) as app:
        auth = await _signed_in(app.client, fake_google)
        monkeypatch.setattr(google_connect, "store", broken)
        failed = await _connect(app.client, fake_google, auth)
        cleared = HANDOFF not in app.client.cookies
    assert (failed.status_code, failed.json()["error"]) == (500, "connect_failed")
    assert "db-7" not in failed.text
    assert cleared
