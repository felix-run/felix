"""AES-256-GCM at-rest helpers."""

from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def encrypt(plaintext: bytes, key_b64: str) -> str:
    key = base64.b64decode(key_b64)
    if len(key) != 32:
        raise ValueError("OAUTH_CACHE_KEY must be 32 bytes base64")
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, plaintext, None)
    return base64.b64encode(nonce + ct).decode()


def decrypt(token_b64: str, key_b64: str) -> bytes:
    key = base64.b64decode(key_b64)
    raw = base64.b64decode(token_b64)
    nonce, ct = raw[:12], raw[12:]
    return AESGCM(key).decrypt(nonce, ct, None)


encrypt_at_rest = encrypt
decrypt_at_rest = decrypt
