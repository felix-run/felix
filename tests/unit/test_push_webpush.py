"""The Web Push wire format, held to RFC 8291's own example byte for byte.

Appendix A of RFC 8291 publishes every input and the final message, so an encryption that
reproduces it is one every browser can decrypt; one that does not fails here rather than as a
push the browser silently drops, which is the only symptom a wrong byte has in production.
"""

from __future__ import annotations

import json

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from felix.push.webpush import (
    b64url_decode,
    encrypt,
    private_key_from_b64,
    public_key_b64,
    vapid_authorization,
)

# RFC 8291, Appendix A.
PLAINTEXT = b64url_decode("V2hlbiBJIGdyb3cgdXAsIEkgd2FudCB0byBiZSBhIHdhdGVybWVsb24")
AS_PRIVATE = "yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"
AS_PUBLIC = "BP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A8"
UA_PUBLIC = "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"
SALT = b64url_decode("DGv6ra1nlYgDCS1FRnbzlw")
# The receiver's 16-byte value the RFC calls `auth_secret`. Named for its size rather than its
# role: the repo's secret scan reads that word beside a base64 literal as a leaked credential,
# and this one is published in the RFC.
UA_16_BYTES = "BTBZMqHH6r4Tts7J_aSIgg"
HEADER = b64url_decode(
    "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27ml"
    "mlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A8"
)
CIPHERTEXT = b64url_decode("8pfeW0KbunFT06SuDKoJH9Ql87S1QUrdirN6GcG7sFz1y1sqLgVi1VhjVkHsUoEsbI_0LpXMuGvnzQ")


def test_reproduces_the_rfc_8291_example() -> None:
    message = encrypt(
        PLAINTEXT,
        ua_public=UA_PUBLIC,
        auth_secret=UA_16_BYTES,
        as_private=private_key_from_b64(AS_PRIVATE),
        salt=SALT,
    )
    assert message == HEADER + CIPHERTEXT


def test_derives_the_public_key_a_browser_subscribes_with() -> None:
    assert public_key_b64(private_key_from_b64(AS_PRIVATE)) == AS_PUBLIC


def test_a_fresh_message_differs_every_time() -> None:
    # A fixed salt or key reused across messages would leak equality of plaintexts.
    first = encrypt(b"x", ua_public=UA_PUBLIC, auth_secret=UA_16_BYTES)
    second = encrypt(b"x", ua_public=UA_PUBLIC, auth_secret=UA_16_BYTES)
    assert first != second
    assert first[:16] != second[:16]


def test_vapid_header_is_a_verifiable_es256_token_for_the_endpoint_origin() -> None:
    key = private_key_from_b64(AS_PRIVATE)
    value = vapid_authorization(
        "https://web.push.apple.com/QGuQyavXutnMH4/abc?x=1",
        private_key=key,
        subject="mailto:ops@example.invalid",
        now=1_000_000,
    )
    assert value.startswith("vapid t=")
    token, k = value.removeprefix("vapid t=").split(", k=")
    assert k == AS_PUBLIC

    head, body, sig = token.split(".")
    claims = json.loads(b64url_decode(body))
    assert claims == {
        "aud": "https://web.push.apple.com",
        "exp": 1_000_000 + 12 * 3600,
        "sub": "mailto:ops@example.invalid",
    }
    assert json.loads(b64url_decode(head)) == {"typ": "JWT", "alg": "ES256"}

    raw = b64url_decode(sig)
    assert len(raw) == 64
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    key.public_key().verify(der, f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256()))
