"""Binary skill assets (`assets/*.png`, `.pdf`, …) as base64 text.

A skill bundle is a `Mapping[str, str]` everywhere — validator, review, security scan —
so a binary asset travels as base64 text, symmetric on upload and download. Only the
encode/decode here needs to know bytes exist.
"""

from __future__ import annotations

import base64
import re

BINARY_ASSET_MIME_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".pdf": "application/pdf",
    ".zip": "application/zip",
}

BINARY_ASSET_EXTENSIONS: tuple[str, ...] = tuple(BINARY_ASSET_MIME_TYPES)

# 5 MiB decoded — generous for the templates, icons and data files the spec describes, small
# enough to keep a bundle cheap to move.
MAX_BINARY_ASSET_BYTES = 5 * 1024 * 1024

_BASE64_RE = re.compile(r"^[A-Za-z0-9+/]*={0,2}\Z")


def is_binary_asset_path(path: str) -> bool:
    return path.lower().endswith(BINARY_ASSET_EXTENSIONS)


def binary_asset_mime_type(path: str) -> str:
    lower = path.lower()
    for ext, mime in BINARY_ASSET_MIME_TYPES.items():
        if lower.endswith(ext):
            return mime
    return "application/octet-stream"


def encode_base64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def decode_base64(text: str) -> bytes:
    return base64.b64decode(text, validate=True)


def base64_decoded_size(text: str) -> int:
    """Decoded byte length without materialising the bytes. Assumes valid, padded base64."""
    if not text:
        return 0
    padding = 2 if text.endswith("==") else 1 if text.endswith("=") else 0
    return len(text) // 4 * 3 - padding


def is_valid_base64(value: str) -> bool:
    return bool(_BASE64_RE.match(value)) and len(value) % 4 == 0


__all__ = [
    "BINARY_ASSET_EXTENSIONS",
    "BINARY_ASSET_MIME_TYPES",
    "MAX_BINARY_ASSET_BYTES",
    "base64_decoded_size",
    "binary_asset_mime_type",
    "decode_base64",
    "encode_base64",
    "is_binary_asset_path",
    "is_valid_base64",
]
