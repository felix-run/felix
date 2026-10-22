"""A browser that receives Web Push, and a fake push service that records what was posted.

Shared by `tests/unit/test_push.py`, `tests/unit/test_notify_tool.py` and `tests/e2e/test_notify.py`.
`capture_pushes` replaces `felix.push.notify.post` -- the network seam -- so encryption, headers and
outcome recording all run for real, and `Browser.decrypt` reads a captured body the way a browser
would (RFC 8291): what a test asserts is what a third party's push service actually carries.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ENDPOINT = "https://web.push.apple.com/QGuQyavXutnMH4/device-1"


def vapid_key() -> str:
    """A fresh VAPID private key, as `FELIX_PUSH_VAPID_PRIVATE_KEY` takes it."""
    from felix.push.webpush import b64url_encode

    key = ec.generate_private_key(ec.SECP256R1())
    return b64url_encode(key.private_numbers().private_value.to_bytes(32, "big"))


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
        from felix.push.webpush import b64url_encode

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

    def to(self, endpoint: str) -> list[dict[str, Any]]:
        return [p for p in self if p["endpoint"] == endpoint]


def capture_pushes(monkeypatch: pytest.MonkeyPatch) -> Sent:
    """Replace the push network seam for the test; every post lands in the returned list."""
    from felix.push import notify

    captured = Sent()

    async def fake_post(settings: Any, endpoint: str, headers: dict[str, str], body: bytes) -> int:
        captured.append({"endpoint": endpoint, "headers": headers, "body": body})
        return captured.status

    monkeypatch.setattr(notify, "post", fake_post)
    return captured


__all__ = ["ENDPOINT", "Browser", "Sent", "capture_pushes", "vapid_key"]
