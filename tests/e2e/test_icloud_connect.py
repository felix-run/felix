"""Connecting a person's iCloud to their vault, over real HTTP: `POST /connections/icloud`.

Boots `create_application()` under `auth_mode=jwt`, with the connectors plugin mounting its route
as production does and iCloud played at a loopback address (`tests/support/icloud_fake`) -- the
one thing replaced is where `Dav.from_settings` points. The person checks in with a self-issued
token; `GET /connections` then shows what was kept, and the vault's own read shows it is the
password Apple accepted. Nothing here writes the vault by hand.
"""

from __future__ import annotations

import base64
import os
from typing import Any

import pytest
from felix.auth import connections
from felix.auth.jwt import mint_token
from joserfc import jwk

from tests.support.icloud_fake import APP_PASSWORD, APPLE_ID, FakeICloud
from tests.support.loopback_http import serve
from tests.support.optional_deps import require_optional

_KEY = jwk.RSAKey.generate_key(2048)
SUB = "github:4242"
PERSON = f"felix-self|{SUB}"


@pytest.fixture(autouse=True)
def _plugin() -> Any:
    require_optional("icalendar", "icloud-connectors")
    return require_optional("felix_connectors", "connectors")


def _env(**overrides: str) -> dict[str, str]:
    env = {
        "FELIX_ENVIRONMENT": "development",
        "FELIX_AUTH_MODE": "jwt",
        "FELIX_JWT_VERIFIERS": "self:felix-self",
        "FELIX_JWKS_PUBLIC": _KEY.as_pem(private=False).decode(),
        "FELIX_JWKS_PRIVATE": _KEY.as_pem(private=True).decode(),
        "FELIX_ALLOWED_TENANTS": "acme",
        "FELIX_CONNECTION_TOKEN_KEY": base64.b64encode(os.urandom(32)).decode(),
    }
    env.update(overrides)
    return env


@pytest.fixture
async def icloud(monkeypatch: pytest.MonkeyPatch) -> Any:
    from felix.security.host_allowlist import url_host
    from felix_connectors.icloud_dav import Dav

    fake = FakeICloud()
    async with serve(fake.caldav) as caldav, serve(fake.carddav) as carddav:
        here = Dav(caldav=caldav, carddav=carddav, hosts=frozenset({url_host(caldav)}), allow_http=True)
        monkeypatch.setattr(Dav, "from_settings", classmethod(lambda cls, settings: here))
        yield fake


def _bearer(settings: Any, sub: str = SUB) -> dict[str, str]:
    token = mint_token(settings, sub=sub, tenant_id="acme", scopes=[], ttl_seconds=600)
    return {"Authorization": f"Bearer {token}"}


async def _connect(app: Any, apple_id: str = "Me@iCloud.com", password: str = APP_PASSWORD) -> Any:
    return await app.client.post(
        "/connections/icloud",
        json={"apple_id": apple_id, "app_password": password},
        headers=_bearer(app.settings),
    )


async def test_a_person_connects_with_an_app_password_apple_accepts(
    boot: Any, icloud: FakeICloud, caplog: pytest.LogCaptureFixture
) -> None:
    async with boot([], env=_env()) as app:
        made = await _connect(app)
        assert made.status_code == 200, made.text
        listed = await app.client.get("/connections", headers=_bearer(app.settings))
        stored = await connections.secret(app.settings, "acme", PERSON, "icloud")
    assert made.json() == {
        "provider": "icloud",
        "account": APPLE_ID,
        "label": APPLE_ID,
        "kind": "secret",
        "status": "active",
        "created_at": made.json()["created_at"],
        "updated_at": made.json()["updated_at"],
    }
    assert [(c["provider"], c["account"], c["kind"]) for c in listed.json()["items"]] == [
        ("icloud", APPLE_ID, "secret")
    ]
    assert stored == APP_PASSWORD
    (check,) = icloud.seen
    assert icloud.served == {"caldav": ["PROPFIND /"], "carddav": []}
    assert check.headers["depth"] == "0"
    assert check.headers["authorization"] == icloud.auth
    assert APP_PASSWORD not in made.text + listed.text + caplog.text


async def test_a_password_apple_refuses_is_not_kept(boot: Any, icloud: FakeICloud) -> None:
    async with boot([], env=_env()) as app:
        refused = await _connect(app, password="zzzz-zzzz-zzzz-zzzz")
        listed = await app.client.get("/connections", headers=_bearer(app.settings))
    assert (refused.status_code, refused.json()["error"]) == (400, "app_password_refused")
    assert listed.json()["items"] == []
    assert len(icloud.seen) == 1


@pytest.mark.parametrize(
    ("apple_id", "password", "code"),
    [
        (APPLE_ID, "my-real-apple-password", "app_password_format"),
        ("me:x@icloud.com", APP_PASSWORD, "apple_id_invalid"),
        ("not-an-email", APP_PASSWORD, "apple_id_invalid"),
    ],
)
async def test_a_malformed_credential_never_reaches_apple(
    boot: Any, icloud: FakeICloud, apple_id: str, password: str, code: str
) -> None:
    async with boot([], env=_env()) as app:
        refused = await _connect(app, apple_id, password)
    assert (refused.status_code, refused.json()["error"]) == (400, code)
    assert password not in refused.text
    assert icloud.seen == []


async def test_icloud_down_is_said_and_nothing_kept(
    boot: Any, icloud: FakeICloud, caplog: pytest.LogCaptureFixture
) -> None:
    icloud.raw[("PROPFIND", "/")] = ("503 Service Unavailable", b"")
    async with boot([], env=_env()) as app:
        down = await _connect(app)
        listed = await app.client.get("/connections", headers=_bearer(app.settings))
    assert (down.status_code, down.json()["error"]) == (502, "icloud_unavailable")
    assert listed.json()["items"] == []
    assert APP_PASSWORD not in caplog.text


async def test_without_a_vault_key_there_is_no_icloud_to_connect(boot: Any, icloud: FakeICloud) -> None:
    async with boot([], env=_env(FELIX_CONNECTION_TOKEN_KEY="")) as app:
        missing = await _connect(app)
    assert missing.status_code == 404
    assert icloud.seen == []


async def test_a_caller_naming_no_person_cannot_connect(boot: Any, icloud: FakeICloud) -> None:
    async with boot([], env=_env()) as app:
        anonymous = await app.client.post(
            "/connections/icloud", json={"apple_id": APPLE_ID, "app_password": APP_PASSWORD}
        )
    assert anonymous.status_code == 401
    assert icloud.seen == []


async def test_a_machine_caller_is_no_person_to_connect(boot: Any, icloud: FakeICloud) -> None:
    async with boot([], env=_env()) as app:
        machine = await app.client.post(
            "/connections/icloud",
            json={"apple_id": APPLE_ID, "app_password": APP_PASSWORD},
            headers=_bearer(app.settings, sub="github-actions:repo:acme/widgets"),
        )
    assert (machine.status_code, machine.json()["detail"]) == (403, "not_a_person")
    assert icloud.seen == []


async def test_disconnecting_forgets_the_password(boot: Any, icloud: FakeICloud) -> None:
    async with boot([], env=_env()) as app:
        assert (await _connect(app)).status_code == 200
        gone = await app.client.delete(f"/connections/icloud/{APPLE_ID}", headers=_bearer(app.settings))
        listed = await app.client.get("/connections", headers=_bearer(app.settings))
    assert gone.status_code == 204
    assert listed.json()["items"] == []
    assert [r.method for r in icloud.seen] == ["PROPFIND"]


@pytest.mark.parametrize(
    "body",
    [
        {"apple_id": APPLE_ID},
        {"apple_id": APPLE_ID, "app_password": APP_PASSWORD, "extra": "x"},
        {"apple_id": APPLE_ID, "app_password": APP_PASSWORD + "-" * 80},
        "not json",
    ],
)
async def test_a_malformed_body_is_refused_without_echoing_it(
    boot: Any, icloud: FakeICloud, body: Any
) -> None:
    async with boot([], env=_env()) as app:
        sent = body if isinstance(body, str) else __import__("json").dumps(body)
        refused = await app.client.post(
            "/connections/icloud",
            content=sent,
            headers={**_bearer(app.settings), "content-type": "application/json"},
        )
    assert (refused.status_code, refused.json()) == (
        400,
        {"error": "invalid_body", "message": "send {apple_id, app_password} as JSON"},
    )
    assert icloud.seen == []


async def test_a_person_may_try_a_few_times_an_hour(boot: Any, icloud: FakeICloud) -> None:
    from felix_connectors.icloud_connect import ATTEMPTS

    async with boot([], env=_env()) as app:
        tries = [await _connect(app, password="zzzz-zzzz-zzzz-zzzz") for _ in range(ATTEMPTS + 1)]
        other = await app.client.post(
            "/connections/icloud",
            json={"apple_id": APPLE_ID, "app_password": APP_PASSWORD},
            headers=_bearer(app.settings, sub="github:5150"),
        )
    assert [t.status_code for t in tries] == [400] * ATTEMPTS + [429]
    assert tries[-1].json()["error"] == "rate_limited"
    assert len(icloud.seen) == ATTEMPTS + 1  # the refused try reached nothing; the other person did
    assert other.status_code == 200


async def test_a_vault_that_cannot_keep_it_says_so(
    boot: Any, icloud: FakeICloud, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def down(*_: Any, **__: Any) -> Any:
        raise connections.StoreUnavailable("the connection store could not be reached just now")

    monkeypatch.setattr(connections, "save_secret", down)
    async with boot([], env=_env()) as app:
        failed = await _connect(app)
    assert (failed.status_code, failed.json()["error"]) == (503, "connect_failed")
    assert APP_PASSWORD not in failed.text
