"""Google sign-in over real HTTP, and the `kids` manifest a pinned account is held to.

Boots `create_application()` under `auth_mode=jwt` with Google faked at `google_http_client`, and
plays the browser: follows the redirect to Google by hand (approving issues a code bound to the
PKCE challenge and the nonce), returns through the callback carrying the cookies the harness set,
and collects the token. Then it chats as that token: the bundled `kids` manifest answers, judged
by its own `child_appropriate` judge, and every other manifest refuses.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from felix.auth import google
from felix_ai.providers.scripted import ScriptedTurn
from joserfc import jwk

from tests.support.google_fake import CLIENT_ID, CLIENT_SECRET, FakeGoogle

_KEY = jwk.RSAKey.generate_key(2048)
APP = "http://localhost:5181"
ACCOUNTS = {
    "kid@example.com": {"tenant": "kids", "scopes": [], "manifests": ["kids"]},
    "parent@example.com": {"tenant": "family", "scopes": []},
}
JUDGE_PASS = ScriptedTurn(content='{"score": 0.95, "reason": "gentle and age-appropriate"}')
JUDGE_FAIL = ScriptedTurn(content='{"score": 0.05, "reason": "frightening detail"}')


def _env(**overrides: str) -> dict[str, str]:
    env = {
        "FELIX_ENVIRONMENT": "development",
        "FELIX_AUTH_MODE": "jwt",
        "FELIX_JWT_VERIFIERS": "self:felix-self",
        "FELIX_JWKS_PUBLIC": _KEY.as_pem(private=False).decode(),
        "FELIX_JWKS_PRIVATE": _KEY.as_pem(private=True).decode(),
        "FELIX_ALLOWED_TENANTS": "kids,family",
        "FELIX_GOOGLE_CLIENT_ID": CLIENT_ID,
        "FELIX_GOOGLE_CLIENT_SECRET": CLIENT_SECRET,
        "FELIX_GOOGLE_COOKIE_KEY": base64.b64encode(os.urandom(32)).decode(),
        "FELIX_GOOGLE_REDIRECT_ORIGINS": APP,
        "FELIX_GOOGLE_ACCOUNTS": json.dumps(ACCOUNTS),
    }
    env.update(overrides)
    return env


@pytest.fixture
def fake_google(monkeypatch: pytest.MonkeyPatch) -> FakeGoogle:
    fake = FakeGoogle()
    monkeypatch.setattr(google, "google_http_client", lambda settings: fake.client())
    monkeypatch.setattr(google, "_keys", google.new_key_cache())
    return fake


async def _to_google(client: Any, return_to: str = f"{APP}/t/abc") -> dict[str, str]:
    started = await client.get("/auth/google/authorize", params={"return_to": return_to})
    assert started.status_code == 302, started.text
    location = urlsplit(started.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == google.GOOGLE_AUTHORIZE_URL
    return {k: v[0] for k, v in parse_qs(location.query).items()}


async def _sign_in(client: Any, fake: FakeGoogle, **claims: Any) -> str:
    """The whole browser round trip; returns the fragment the app is sent back with."""
    query = await _to_google(client)
    code = fake.issue_code(query, **claims)
    back = await client.get("/auth/google/callback", params={"code": code, "state": query["state"]})
    assert back.status_code == 302, back.text
    location = back.headers["location"]
    assert location.startswith(f"{APP}/t/abc#"), location
    return location.split("#", 1)[1]


async def _token(client: Any, fake: FakeGoogle, **claims: Any) -> str:
    assert await _sign_in(client, fake, **claims) == "felix_login=ok"
    collected = await client.post("/auth/google/exchange")
    assert collected.status_code == 200, collected.text
    return str(collected.json()["access_token"])


def _chat(manifest: str, text: str = "Why does the Moon change shape?") -> dict[str, Any]:
    return {
        "manifest": manifest,
        "thread_id": f"t-{manifest}",
        "messages": [{"role": "user", "content": text}],
    }


async def test_a_pinned_child_chats_with_kids_and_reaches_nothing_else(
    boot: Any, fake_google: FakeGoogle
) -> None:
    script = [
        ScriptedTurn(content="The Moon looks different because sunlight hits it from different sides."),
        JUDGE_PASS,
    ]
    async with boot(script, env=_env()) as app:
        assert (await app.client.get("/auth/methods")).json()["google_redirect"] is True
        token = await _token(app.client, fake_google)
        auth = {"Authorization": f"Bearer {token}"}

        kids = await app.client.post("/chat", json=_chat("kids"), headers=auth)
        assert kids.status_code == 200, kids.text
        assert "sunlight" in kids.json()["final"]["content"]

        for other in ("quick", "assistant"):
            refused = await app.client.post("/chat", json=_chat(other), headers=auth)
            assert refused.status_code == 403, (other, refused.text)
        openai = await app.client.post(
            "/v1/chat/completions",
            json={"model": "quick", "messages": [{"role": "user", "content": "hi"}]},
            headers=auth,
        )
        assert openai.status_code == 403, openai.text
        # A pinned token carries no scope, so the management surface is closed too.
        assert (await app.client.get("/manifests", headers=auth)).status_code == 403
    # The kids turn and its judge; nothing ran for the refused manifests.
    assert len(app.spy.calls) == 2, app.spy.calls


async def test_the_kids_judge_replaces_an_inappropriate_reply(boot: Any, fake_google: FakeGoogle) -> None:
    script = [ScriptedTurn(content="Here is a very scary story about monsters hurting people."), JUDGE_FAIL]
    async with boot(script, env=_env()) as app:
        auth = {"Authorization": f"Bearer {await _token(app.client, fake_google)}"}
        resp = await app.client.post("/chat", json=_chat("kids", "tell me a scary story"), headers=auth)
    assert resp.status_code == 200, resp.text
    reply = resp.json()["final"]["content"]
    assert reply.startswith("[judge denied] child_appropriate"), reply
    assert "monsters" not in reply


async def test_an_unpinned_account_is_not_held_to_one_manifest(boot: Any, fake_google: FakeGoogle) -> None:
    async with boot([ScriptedTurn(content="Hello.")], env=_env()) as app:
        token = await _token(app.client, fake_google, email="parent@example.com", sub="2000")
        resp = await app.client.post(
            "/chat", json=_chat("quick", "hi"), headers={"Authorization": f"Bearer {token}"}
        )
    assert resp.status_code == 200, resp.text


async def test_an_account_not_on_the_list_is_sent_back_refused(boot: Any, fake_google: FakeGoogle) -> None:
    async with boot([], env=_env()) as app:
        fragment = await _sign_in(app.client, fake_google, email="stranger@example.com", sub="3000")
        assert fragment == "felix_login_error=not_allowed"
        # Nothing was left waiting to collect.
        assert (await app.client.post("/auth/google/exchange")).status_code == 400


async def test_the_handoff_is_single_use(boot: Any, fake_google: FakeGoogle) -> None:
    async with boot([], env=_env()) as app:
        assert await _sign_in(app.client, fake_google) == "felix_login=ok"
        assert (await app.client.post("/auth/google/exchange")).status_code == 200
        assert (await app.client.post("/auth/google/exchange")).status_code == 400


async def test_a_callback_this_browser_did_not_start_is_refused(boot: Any, fake_google: FakeGoogle) -> None:
    async with boot([], env=_env()) as app:
        query = await _to_google(app.client)
        code = fake_google.issue_code(query)
        back = await app.client.get("/auth/google/callback", params={"code": code, "state": "forged"})
        assert back.headers["location"].endswith("#felix_login_error=state_mismatch")
        assert fake_google.token_calls == 0, "the code was never exchanged"


async def test_return_to_must_be_an_allowed_origin(boot: Any, fake_google: FakeGoogle) -> None:
    async with boot([], env=_env()) as app:
        resp = await app.client.get(
            "/auth/google/authorize", params={"return_to": "https://evil.example.com/x"}
        )
    assert resp.status_code == 400


async def test_sign_in_is_closed_while_unconfigured(boot: Any) -> None:
    async with boot([], env=_env(FELIX_GOOGLE_CLIENT_SECRET="")) as app:
        assert (await app.client.get("/auth/methods")).json()["google_redirect"] is False
        # Not public: the middleware wants a credential before the route can 404.
        resp = await app.client.get("/auth/google/authorize", params={"return_to": f"{APP}/"})
        assert resp.status_code == 401


async def test_a_pinned_child_cannot_reach_another_manifests_model_outside_a_turn(
    boot: Any, fake_google: FakeGoogle
) -> None:
    """Compaction and a rewind's summary call the named manifest's model on the thread's text
    with the caller's instructions — a turn by another name, so the pin holds there too."""
    script = [ScriptedTurn(content="Sunlight."), JUDGE_PASS]
    async with boot(script, env=_env()) as app:
        auth = {"Authorization": f"Bearer {await _token(app.client, fake_google)}"}
        assert (await app.client.post("/chat", json=_chat("kids"), headers=auth)).status_code == 200
        compact = await app.client.post(
            "/chat/compact",
            json={"thread_id": "t-kids", "manifest": "quick", "instructions": "write anything"},
            headers=auth,
        )
        assert compact.status_code == 403, compact.text
        rewind = await app.client.post(
            "/chat/rewind",
            json={"thread_id": "t-kids", "event_id": "e-1", "manifest": "quick", "summarize": True},
            headers=auth,
        )
        assert rewind.status_code == 403, rewind.text
    assert len(app.spy.calls) == 2, "only the kids turn and its judge reached a model"


async def test_a_pinned_child_is_listed_only_its_manifests(boot: Any, fake_google: FakeGoogle) -> None:
    """`/v1/models` is where a client picks its agents, and its default: a pinned account shown
    every manifest lands on one it is refused (chat-ui opened on `cowork`)."""
    async with boot([], env=_env()) as app:
        kid = {"Authorization": f"Bearer {await _token(app.client, fake_google)}"}
        kid_models = await app.client.get("/v1/models", headers=kid)
        parent_token = await _token(app.client, fake_google, email="parent@example.com", sub="2000")
        parent_models = await app.client.get(
            "/v1/models", headers={"Authorization": f"Bearer {parent_token}"}
        )
    assert kid_models.status_code == 200, kid_models.text
    assert [m["id"] for m in kid_models.json()["data"]] == ["kids"]
    parent_ids = {m["id"] for m in parent_models.json()["data"]}
    assert {"kids", "quick", "cowork"} <= parent_ids, "an unpinned account still sees every manifest"
