"""The Web Push wire format: RFC 8291 message encryption and an RFC 8292 VAPID header.

Written against `cryptography`, which the harness already depends on, rather than taken from
`pywebpush`: that library sends with the synchronous `requests` (blocking I/O on the event
loop every stream shares) through its own client, around the egress guard -- and a push
endpoint is a URL a browser handed us. Sending is `felix.push.notify`'s job, through
`safe_async_client`; this module only builds bytes and headers, and does no I/O.

`tests/unit/test_push_webpush.py` holds it to RFC 8291's Appendix A vector byte for byte.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import struct
import time
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# One record holds the whole message: push services cap a payload near 4 KiB, and ours are a
# few hundred bytes.
RECORD_SIZE = 4096
# How long a VAPID token is good for. RFC 8292 caps it at 24 hours; push services reject one
# that is too far out, so stay well inside.
VAPID_TTL_SECONDS = 12 * 60 * 60


def b64url_decode(value: str) -> bytes:
    """Decode base64url with or without padding, as browsers and key generators both emit."""
    value = value.strip()
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _hmac_sha256(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()


def _public_bytes(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def private_key_from_b64(raw: str) -> ec.EllipticCurvePrivateKey:
    """A P-256 private key from its 32-byte scalar, base64url -- the form VAPID tools print."""
    scalar = b64url_decode(raw)
    if len(scalar) != 32:
        raise ValueError("expected a base64url P-256 private key (32 bytes)")
    return ec.derive_private_key(int.from_bytes(scalar, "big"), ec.SECP256R1())


def public_key_b64(private_key: ec.EllipticCurvePrivateKey) -> str:
    """The application server key a browser subscribes with (`applicationServerKey`)."""
    return b64url_encode(_public_bytes(private_key.public_key()))


def validate_subscription_keys(p256dh: str, auth: str) -> None:
    """Raise `ValueError` unless these are keys a message could be encrypted to.

    A point on P-256 and a 16-byte auth secret, base64url, as `PushSubscription.toJSON()` hands
    them over. Anything else would only show up as every later send to that row failing.
    """
    point, secret = b64url_decode(p256dh), b64url_decode(auth)
    if len(secret) != 16:
        raise ValueError("auth secret must be 16 bytes")
    ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), point)


def encrypt(
    plaintext: bytes,
    *,
    ua_public: str,
    auth_secret: str,
    as_private: ec.EllipticCurvePrivateKey | None = None,
    salt: bytes | None = None,
) -> bytes:
    """Encrypt one push message to a browser (RFC 8291, `aes128gcm`).

    `as_private` and `salt` are fresh per message; they are parameters only so the RFC's
    vector can be reproduced.
    """
    ua_key_bytes = b64url_decode(ua_public)
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_key_bytes)
    auth = b64url_decode(auth_secret)
    as_private = as_private or ec.generate_private_key(ec.SECP256R1())
    as_public = _public_bytes(as_private.public_key())
    salt = salt if salt is not None else os.urandom(16)

    ecdh_secret = as_private.exchange(ec.ECDH(), ua_key)
    # HKDF(auth_secret, ecdh_secret, "WebPush: info" || 0x00 || ua_public || as_public, 32)
    prk_key = _hmac_sha256(auth, ecdh_secret)
    key_info = b"WebPush: info\x00" + ua_key_bytes + as_public
    ikm = _hmac_sha256(prk_key, key_info + b"\x01")
    # HKDF(salt, IKM, "Content-Encoding: aes128gcm" || 0x00, 16) and the nonce likewise, 12.
    prk = _hmac_sha256(salt, ikm)
    cek = _hmac_sha256(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = _hmac_sha256(prk, b"Content-Encoding: nonce\x00\x01")[:12]

    # The last (and only) record ends with the 0x02 delimiter and carries no padding.
    ciphertext = AESGCM(cek).encrypt(nonce, plaintext + b"\x02", None)
    header = salt + struct.pack("!IB", RECORD_SIZE, len(as_public)) + as_public
    return header + ciphertext


def vapid_authorization(
    endpoint: str,
    *,
    private_key: ec.EllipticCurvePrivateKey,
    subject: str,
    now: float | None = None,
) -> str:
    """The `Authorization` header that identifies this harness to the push service (RFC 8292).

    The audience is the endpoint's origin, which is the push service rather than the browser;
    the subject is the operator's contact, which push services use to reach someone about
    abuse before they block the key. Signing is cheap, so each send signs its own.
    """
    parts = urlsplit(endpoint)
    claims = {
        # The origin as the push service names itself: scheme and host, never a port or
        # userinfo (`host_allowed` refuses endpoints carrying either).
        "aud": f"{parts.scheme}://{parts.hostname}",
        "exp": int((now if now is not None else time.time()) + VAPID_TTL_SECONDS),
        "sub": subject,
    }
    header = {"typ": "JWT", "alg": "ES256"}
    signing_input = (
        b64url_encode(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + b64url_encode(json.dumps(claims, separators=(",", ":")).encode())
    )
    der = private_key.sign(signing_input.encode("ascii"), ec.ECDSA(hashes.SHA256()))
    # JWS ES256 is the raw r || s, not the DER that `sign` returns.
    r, s = decode_dss_signature(der)
    signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    token = f"{signing_input}.{b64url_encode(signature)}"
    return f"vapid t={token}, k={public_key_b64(private_key)}"


__all__ = [
    "RECORD_SIZE",
    "VAPID_TTL_SECONDS",
    "b64url_decode",
    "b64url_encode",
    "encrypt",
    "private_key_from_b64",
    "public_key_b64",
    "validate_subscription_keys",
    "vapid_authorization",
]
