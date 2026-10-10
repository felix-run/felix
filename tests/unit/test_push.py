"""Web Push: who may subscribe, where a push may go, and what one carries.

The routes run through `create_app`, for the scope gate and the tenant. The sends are driven
through the real `create_pending`, because the rule that matters most -- one push per new
approval, none for a reused row -- lives in which branch of that function announces. Each
captured push is decrypted the way a browser would, so what is asserted is what a third
party's push service actually carries.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from felix.config import Settings
from felix.push import notify
from felix.push import store as push_store
from felix.push.webpush import b64url_decode, b64url_encode
from httpx import ASGITransport, AsyncClient

KEYS = (
    '{"sk-ops":{"tenant_id":"acme","sub":"ops","scopes":["approvals:read"]},'
    '"sk-chat":{"tenant_id":"acme","sub":"ops","scopes":["chat:write"]},'
    '"sk-other":{"tenant_id":"globex","sub":"ops","scopes":["approvals:read"]}}'
)
ENDPOINT = "https://web.push.apple.com/QGuQyavXutnMH4/device-1"


def _vapid_key() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    return b64url_encode(key.private_numbers().private_value.to_bytes(32, "big"))


def _settings(**kw: object) -> Settings:
    base: dict[str, object] = {
        "allow_insecure": True,
        "auth_mode": "api_key",
        "auth_api_keys": KEYS,
        "environment": "development",
        "object_store": "memory",
        "database_url": "memory://push",
        "push_vapid_private_key": _vapid_key(),
        "push_vapid_subject": "mailto:ops@example.invalid",
    }
    base.update(kw)
    return Settings(**base)  # type: ignore[arg-type]


async def _client(**kw: object) -> AsyncClient:
    from felix_api.app import create_app

    app = create_app(settings=_settings(**kw), plugins=[])
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


class Browser:
    """The receiving end: a key pair and auth secret, and RFC 8291 decryption."""

    def __init__(self, endpoint: str = ENDPOINT) -> None:
        self.endpoint = endpoint
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.public = self.key.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        self.auth = b"0123456789abcdef"

    def subscription(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "expirationTime": None,
            "keys": {"p256dh": b64url_encode(self.public), "auth": b64url_encode(self.auth)},
        }

    def decrypt(self, body: bytes) -> dict[str, Any]:
        salt, idlen = body[:16], body[20]
        as_public = body[21 : 21 + idlen]
        sender = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public)
        secret = self.key.exchange(ec.ECDH(), sender)

        def mac(k: bytes, d: bytes) -> bytes:
            return hmac.new(k, d, hashlib.sha256).digest()

        ikm = mac(mac(self.auth, secret), b"WebPush: info\x00" + self.public + as_public + b"\x01")
        prk = mac(salt, ikm)
        cek = mac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
        nonce = mac(prk, b"Content-Encoding: nonce\x00\x01")[:12]
        plain = AESGCM(cek).decrypt(nonce, body[21 + idlen :], None)
        assert plain.endswith(b"\x02")
        return json.loads(plain[:-1])


class Sent(list[dict[str, Any]]):
    """Every push the harness posted, and the status the fake push service answers with."""

    status = 201


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> Sent:
    captured = Sent()

    async def fake_post(settings: Settings, endpoint: str, headers: dict[str, str], body: bytes) -> int:
        captured.append({"endpoint": endpoint, "headers": headers, "body": body})
        return captured.status

    monkeypatch.setattr(notify, "post", fake_post)
    return captured


async def _subscribe(settings: Settings, browser: Browser, tenant: str = "acme") -> None:
    sub = browser.subscription()
    await push_store.upsert(
        settings, tenant, endpoint=browser.endpoint, p256dh=sub["keys"]["p256dh"], auth=sub["keys"]["auth"]
    )


async def _drain() -> None:
    while notify._in_flight:
        await asyncio.gather(*list(notify._in_flight), return_exceptions=True)


async def _create_pending(settings: Settings, **kw: Any) -> dict[str, Any]:
    from felix.approvals import store as approvals_store

    args: dict[str, Any] = {
        "tool_name": "write_file",
        "call_signature": "sig-1",
        "args": {"path": "secret-plans.md", "content": "the whole file"},
        "ttl_seconds": 300,
        "thread_id": "acme:thread-7",
        "reason": "Confirm writes to the workspace",
    }
    args.update(kw)
    return await approvals_store.create_pending(settings, "acme", **args)


# --- routes ---


async def test_routes_say_push_is_off_until_a_key_is_set() -> None:
    # Leaving is the exception: `test_a_browser_can_leave_after_push_is_turned_off`.
    async with await _client(push_vapid_private_key="", push_vapid_subject="") as client:
        for method, path, body in [
            ("GET", "/push/vapid-public-key", None),
            ("POST", "/push/subscriptions", Browser().subscription()),
        ]:
            res = await client.request(method, path, json=body, headers=_auth("sk-ops"))
            assert res.status_code == 503, path
            assert res.json()["detail"] == "push_not_configured"


async def test_subscribing_needs_the_scope_the_approval_frame_needs() -> None:
    async with await _client() as client:
        res = await client.post(
            "/push/subscriptions", json=Browser().subscription(), headers=_auth("sk-chat")
        )
        assert res.status_code == 403
        assert "approvals:read" in res.json()["detail"]


async def test_serves_the_public_half_of_the_configured_key() -> None:
    raw = _vapid_key()
    async with await _client(push_vapid_private_key=raw) as client:
        res = await client.get("/push/vapid-public-key", headers=_auth("sk-ops"))
    assert res.status_code == 200
    key = ec.derive_private_key(int.from_bytes(b64url_decode(raw), "big"), ec.SECP256R1())
    expected = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    assert b64url_decode(res.json()["public_key"]) == expected


async def test_subscribing_is_idempotent_and_never_echoes_the_endpoint() -> None:
    browser = Browser()
    async with await _client() as client:
        first = await client.post("/push/subscriptions", json=browser.subscription(), headers=_auth("sk-ops"))
        again = await client.post("/push/subscriptions", json=browser.subscription(), headers=_auth("sk-ops"))
    assert first.status_code == again.status_code == 200
    assert first.json()["id"] == again.json()["id"]
    assert ENDPOINT not in first.text
    assert len(await push_store.list_for_tenant(_settings(), "acme")) == 1


async def test_refuses_an_endpoint_off_the_push_service_list() -> None:
    # The endpoint is a URL a browser hands over; without the list, anyone allowed to
    # subscribe could aim the harness's POSTs anywhere the egress guard lets through.
    async with await _client() as client:
        for endpoint in [
            "https://attacker.example/collect",
            "http://web.push.apple.com/x",
            "https://web.push.apple.com:8443/x",
            "https://push.apple.com.attacker.example/x",
            # Not what a browser produces: refused rather than normalised, since each would
            # send a token or credentials the push service rejects on every push.
            "https://user:pw@fcm.googleapis.com/fcm/send/x",
            "https://fcm.googleapis.com:443/fcm/send/x",
            "https://FCM.googleapis.com/fcm/send/x",
            "https://fcm.googleapis.com:abc/fcm/send/x",
            "https://web.push.apple.com./x",
        ]:
            sub = {**Browser().subscription(), "endpoint": endpoint}
            res = await client.post("/push/subscriptions", json=sub, headers=_auth("sk-ops"))
            assert res.status_code == 422, endpoint
            assert res.json()["detail"] == "push_endpoint_not_allowed"


async def test_refuses_keys_nothing_could_be_encrypted_to() -> None:
    async with await _client() as client:
        sub = Browser().subscription()
        for bad in (b"\x04" + b"x" * 10, b"\x04" + b"\x07" * 64):  # short; right length, off the curve
            sub["keys"]["p256dh"] = b64url_encode(bad)
            res = await client.post("/push/subscriptions", json=sub, headers=_auth("sk-ops"))
            assert res.status_code == 422, bad[:4]
            assert res.json()["detail"] == "push_keys_malformed"
        for auth in (b64url_encode(b"x" * 15), b64url_encode(b"x" * 17), "a"):  # short, long, not base64url
            sub = Browser().subscription()
            sub["keys"]["auth"] = auth
            res = await client.post("/push/subscriptions", json=sub, headers=_auth("sk-ops"))
            assert res.status_code == 422, auth
            assert res.json()["detail"] == "push_keys_malformed"


async def test_one_tenant_cannot_unsubscribe_another() -> None:
    browser = Browser()
    async with await _client() as client:
        await client.post("/push/subscriptions", json=browser.subscription(), headers=_auth("sk-ops"))
        other = await client.request(
            "DELETE", "/push/subscriptions", json={"endpoint": ENDPOINT}, headers=_auth("sk-other")
        )
        assert other.json() == {"removed": False}
        own = await client.request(
            "DELETE", "/push/subscriptions", json={"endpoint": ENDPOINT}, headers=_auth("sk-ops")
        )
        assert own.json() == {"removed": True}


async def test_a_browser_can_leave_after_push_is_turned_off() -> None:
    browser = Browser()
    sub = browser.subscription()
    await push_store.upsert(
        _settings(), "acme", endpoint=ENDPOINT, p256dh=sub["keys"]["p256dh"], auth=sub["keys"]["auth"]
    )
    async with await _client(push_vapid_private_key="", push_vapid_subject="") as client:
        res = await client.request(
            "DELETE", "/push/subscriptions", json={"endpoint": ENDPOINT}, headers=_auth("sk-ops")
        )
    assert res.json() == {"removed": True}


# --- sends ---


async def test_a_new_approval_wakes_each_browser_once_without_its_arguments(sent: Sent) -> None:
    settings = _settings()
    phone, laptop = Browser(), Browser("https://fcm.googleapis.com/fcm/send/laptop")
    for b in (phone, laptop):
        sub = b.subscription()
        await push_store.upsert(
            settings, "acme", endpoint=b.endpoint, p256dh=sub["keys"]["p256dh"], auth=sub["keys"]["auth"]
        )

    row = await _create_pending(settings)
    await _drain()

    assert sorted(p["endpoint"] for p in sent) == sorted([phone.endpoint, laptop.endpoint])
    push = next(p for p in sent if p["endpoint"] == phone.endpoint)
    message = phone.decrypt(push["body"])
    assert message == {
        "kind": "approval",
        "approval_id": row["id"],
        "tool_name": "write_file",
        "thread_id": "acme:thread-7",
        "expires_at": row["expires_at"],
    }
    # Crossing a third party's servers: no arguments, no reason, no file contents.
    assert push["headers"]["TTL"] == "300"
    assert push["headers"]["Content-Encoding"] == "aes128gcm"
    assert push["headers"]["Authorization"].startswith("vapid t=")


async def test_a_reused_approval_row_does_not_push_again(sent: Sent) -> None:
    settings = _settings()
    b = Browser()
    sub = b.subscription()
    await push_store.upsert(
        settings, "acme", endpoint=b.endpoint, p256dh=sub["keys"]["p256dh"], auth=sub["keys"]["auth"]
    )

    first = await _create_pending(settings)
    again = await _create_pending(settings)  # byte-identical call: same row
    await _drain()

    assert again["id"] == first["id"]
    assert len(sent) == 1


async def test_a_browser_the_push_service_says_is_gone_is_forgotten(sent: Sent) -> None:
    settings = _settings()
    b = Browser()
    sub = b.subscription()
    await push_store.upsert(
        settings, "acme", endpoint=b.endpoint, p256dh=sub["keys"]["p256dh"], auth=sub["keys"]["auth"]
    )
    sent.status = 410

    await _create_pending(settings)
    await _drain()

    assert await push_store.list_for_tenant(settings, "acme") == []


async def test_a_host_dropped_from_the_list_is_not_pushed_to(sent: Sent) -> None:
    b = Browser()
    sub = b.subscription()
    await push_store.upsert(
        _settings(), "acme", endpoint=b.endpoint, p256dh=sub["keys"]["p256dh"], auth=sub["keys"]["auth"]
    )
    narrowed = _settings(push_allowed_hosts="fcm.googleapis.com")

    await _create_pending(narrowed)
    await _drain()

    assert sent == []
    assert await push_store.list_for_tenant(narrowed, "acme") == []


async def test_nothing_is_sent_while_push_is_off(sent: Sent) -> None:
    off = _settings(push_vapid_private_key="", push_vapid_subject="")
    b = Browser()
    sub = b.subscription()
    await push_store.upsert(
        off, "acme", endpoint=b.endpoint, p256dh=sub["keys"]["p256dh"], auth=sub["keys"]["auth"]
    )

    await _create_pending(off)
    # Nothing scheduled at all -- not a send that failed quietly for want of a key.
    assert not notify._in_flight
    await _drain()

    assert sent == []


async def test_a_question_says_one_is_waiting_without_the_question(sent: Sent) -> None:
    # Driven through `request_ui` under a request context, as the agent loop calls it: the
    # tenant and the settings have to come from that context, not from the environment.
    from felix.context import AuthContext, RequestContext, run_with_context
    from felix.ui.prompts import request_ui

    settings = _settings()
    b = Browser()
    sub = b.subscription()
    await push_store.upsert(
        settings, "acme", endpoint=b.endpoint, p256dh=sub["keys"]["p256dh"], auth=sub["keys"]["auth"]
    )

    ctx = RequestContext(settings=settings, auth=AuthContext(tenant_id="acme"), thread_id="acme:thread-7")
    with run_with_context(ctx):
        answer = await request_ui(
            "acme:thread-7", "confirm", prompt="Delete the staging bucket?", timeout=0.01
        )
    await _drain()

    assert answer.cancelled
    (push,) = sent
    message = b.decrypt(push["body"])
    # The thread, and nothing that answers it: not the prompt, not the request id.
    assert message == {"kind": "question", "thread_id": "acme:thread-7"}


async def test_a_subscription_that_keeps_failing_is_dropped_and_one_failure_is_not(sent: Sent) -> None:
    settings = _settings()
    b = Browser()
    sub = b.subscription()
    await push_store.upsert(
        settings, "acme", endpoint=b.endpoint, p256dh=sub["keys"]["p256dh"], auth=sub["keys"]["auth"]
    )
    sent.status = 400

    await notify.send_to_tenant(settings, "acme", {"kind": "question", "thread_id": ""}, ttl=60)
    (row,) = await push_store.list_for_tenant(settings, "acme")
    assert row["failures"] == 1

    for _ in range(push_store.MAX_CONSECUTIVE_FAILURES - 1):
        await notify.send_to_tenant(settings, "acme", {"kind": "question", "thread_id": ""}, ttl=60)
    assert await push_store.list_for_tenant(settings, "acme") == []


async def test_an_approval_reaches_only_its_own_tenant(sent: Sent) -> None:
    settings = _settings()
    ours, theirs = Browser(), Browser("https://fcm.googleapis.com/fcm/send/globex-laptop")
    await _subscribe(settings, ours, "acme")
    await _subscribe(settings, theirs, "globex")

    await _create_pending(settings)  # in acme
    await _drain()

    assert [p["endpoint"] for p in sent] == [ours.endpoint]


@pytest.mark.parametrize(("ttl_seconds", "expected"), [(90, "90"), (5, "30"), (None, "300")])
async def test_a_push_is_held_for_the_approval_s_own_deadline(
    sent: Sent, ttl_seconds: int | None, expected: str
) -> None:
    # Past the deadline the harness has already denied the call by timeout, so a push the
    # service delivers later would announce a decision nobody can make. Clamped to 30s so a
    # very short ttl still reaches a phone, and the harness's own 300s wait with no ttl.
    settings = _settings()
    await _subscribe(settings, Browser())

    await _create_pending(settings, ttl_seconds=ttl_seconds)
    await _drain()

    (push,) = sent
    assert push["headers"]["TTL"] == expected


async def test_a_question_asked_outside_a_request_pushes_nothing(sent: Sent) -> None:
    # No context means no tenant to tell -- a worker-side caller with nothing bound.
    from felix.ui.prompts import request_ui

    await _subscribe(_settings(), Browser())
    await request_ui("acme:thread-7", "confirm", prompt="Proceed?", timeout=0.01)
    assert not notify._in_flight
    assert sent == []


# --- configuration ---


def test_half_configured_push_fails_the_boot() -> None:
    with pytest.raises(RuntimeError, match="set together"):
        _settings(push_vapid_subject="").validate_runtime()
    with pytest.raises(RuntimeError, match="mailto: or https:"):
        _settings(push_vapid_subject="ops@example.invalid").validate_runtime()
    with pytest.raises(RuntimeError, match="FELIX_PUSH_VAPID_PRIVATE_KEY"):
        _settings(push_vapid_private_key="not-a-key").validate_runtime()


def test_a_secret_ref_key_is_left_for_send_time() -> None:
    # Resolving needs the secrets backend, which boot validation does not reach; the shape
    # checks still apply to the subject.
    _settings(push_vapid_private_key="secret:FELIX_PUSH_VAPID_PRIVATE_KEY").validate_runtime()
