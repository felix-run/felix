"""Standard Webhooks signing, shared by the side that sends and the side that receives.

Felix signs its completion webhooks with this (`durability/webhooks.py`) and verifies inbound
triggers with it (`felix/triggers.py`), so the two can never disagree about the scheme: the
signature is `v1,` + base64 HMAC-SHA256 over `"{id}.{timestamp}." + body`, keyed by the decoded
`whsec_` value or, for any other string, its bytes. No I/O here, so a receiver need not import
the sender's HTTP client, store and retry loop to check a signature.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac

WHSEC_PREFIX = "whsec_"


def signing_key(secret: str) -> bytes:
    """The HMAC key a secret names. `ValueError` for a `whsec_` value that is not base64.

    Whitespace in the base64 part is dropped first: a value read back from a secrets manager often
    ends in a newline (`echo ... | gcloud secrets create --data-file=-`), and the sender decoded
    those before this was shared with the receiver. Anything else outside the alphabet refuses."""
    if not secret.startswith(WHSEC_PREFIX):
        return secret.encode()
    try:
        return base64.b64decode("".join(secret.removeprefix(WHSEC_PREFIX).split()), validate=True)
    except binascii.Error:
        raise ValueError("a `whsec_` secret is not valid base64") from None


def signature(key: bytes, message_id: str, timestamp: int, body: bytes) -> str:
    """The `webhook-signature` value for one message, under an HMAC key already decoded."""
    mac = hmac.new(key, f"{message_id}.{timestamp}.".encode() + body, hashlib.sha256).digest()
    return "v1," + base64.b64encode(mac).decode()


def sign(secret: str, message_id: str, timestamp: int, body: bytes) -> str:
    """The `webhook-signature` value for one message, under the key `secret` names."""
    return signature(signing_key(secret), message_id, timestamp, body)


__all__ = ["WHSEC_PREFIX", "sign", "signature", "signing_key"]
