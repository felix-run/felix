"""Caller-uploaded files, stored once and referenced by id.

An image can already be sent inline, as a `data:` URL in a content part. That works and
it is the wrong shape for anything sent twice: the bytes land in the session event log,
and `full_replay` sends the whole log again on every subsequent turn — so a 700 KiB
screenshot attached on turn one is re-uploaded to the model on turns two, three and four.
The 1 MiB body limit bounds each *request* and nothing bounds the thread.

So: store the bytes here, hand back an id, and let a message carry the id. The reference
is small enough to replay.

This module is the storage half. Resolving a `file_id` in a message back to bytes belongs
at the wire, not here — deliberately late, so the log keeps holding the reference rather
than the base64 it expands to.

The key layout, the id format and the containment check all follow `felix.artifacts`,
which learned them the hard way: a charset check is an *argument* that traversal is
impossible, and that argument has already been wrong once here.
"""

from __future__ import annotations

import base64
import logging
import posixpath
import re
import uuid
from dataclasses import dataclass
from typing import Any

from felix.logging_setup import loggable

logger = logging.getLogger("felix.attachments")

# Same shape as a spilled artifact id: uuid4 hex, so a reference cannot be anything but
# one, and cannot be mistaken for a path.
_ID = re.compile(r"\A[0-9a-f]{32}\Z")

# What a model can actually be shown. An allowlist rather than a denylist, because the
# bytes are handed to a vendor API that decides for itself what to do with them, and the
# failure of guessing wrong is a 400 from someone else's server with our request id on it.
#
# `image/*` only for now: that is what both wires encode (`felix_ai.wire.*`), and a
# capability nothing can consume is worse than a missing one. A `application/pdf` arm
# belongs with the reader that would use it.
# Each type paired with how its bytes actually start, because the media type is a caller
# *assertion* and nothing downstream re-derives it. Without this, `/files` is a
# general-purpose blob host wearing an image allowlist: any bytes at all stored as
# `image/png`, and the default filesystem store discards the content type, so not even the
# claim survives. The follow-up resolver would then have to trust a type from the caller's
# message — attacker-controlled twice over.
_MAGIC: dict[str, tuple[bytes, ...]] = {
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/gif": (b"GIF87a", b"GIF89a"),
    # RIFF....WEBP — the four size bytes in between are why this one is checked in halves.
    "image/webp": (b"RIFF",),
}
ALLOWED_MEDIA_TYPES = frozenset(_MAGIC)

# A media type is a token, not a payload. Unbounded, a 1 MiB one fits inside the body limit
# and comes back reflected in the refusal.
MAX_MEDIA_TYPE_CHARS = 127

# Below `CORE_BODY_LIMIT_BYTES` (1 MiB), and for the same reason `documents.py` sits below
# it: set above, this advertises a size the server will never accept, because the
# body-limit middleware answers 413 before the route is reached and the caller is told the
# request was too large without being told what the ceiling actually is.
#
# The margin is bigger here than there because the transport inflates. Bytes arrive
# base64-encoded inside a JSON envelope — four characters per three bytes, plus the
# envelope itself — so a 600 KiB image is already ~800 KiB on the wire.
MAX_ATTACHMENT_BYTES = 600 * 1024


@dataclass(slots=True)
class Attachment:
    """What a caller gets back, and what a later turn resolves."""

    file_id: str
    media_type: str
    size_bytes: int
    filename: str = ""


def attachment_key(tenant_id: str, file_id: str) -> str:
    """Where an upload lives. One definition, used to write and to read."""
    return f"attachments/{tenant_id}/{file_id}"


def valid_file_id(file_id: str) -> bool:
    """Whether this is safe to build a key from.

    Two independent checks, as in `felix.artifacts`. The pattern says what a reference may
    look like; `_contained` says what the result must be regardless — that the key still
    resolves under this tenant's prefix once normalised. The second is this layer's own
    guarantee: the filesystem store makes a similar check about its root, the others never
    normalise at all, and none of them knows what a tenant is.
    """
    if not _ID.match(file_id):
        return False
    return _contained("t", attachment_key("t", file_id))


def _contained(tenant_id: str, key: str) -> bool:
    prefix = f"attachments/{tenant_id}/"
    return posixpath.normpath(key).startswith(prefix) and ".." not in key.split("/")


class AttachmentError(ValueError):
    """A refusal the caller can act on: too large, or a type nothing can read."""


def decode_upload(data_b64: str, media_type: str) -> bytes:
    """Validate and decode an upload, or say why not.

    Decoded before measuring rather than after: base64 of 900 KiB is 1.2 MB of text, and
    the size a caller cares about — and that the model is charged for — is the decoded one.
    `validate=True` so a body that is not base64 is a refusal here rather than silently
    truncated bytes stored under an id that resolves.
    """
    if len(media_type) > MAX_MEDIA_TYPE_CHARS:
        raise AttachmentError(f"media_type exceeds {MAX_MEDIA_TYPE_CHARS} characters")
    if media_type not in ALLOWED_MEDIA_TYPES:
        raise AttachmentError(
            f"media_type {media_type!r} is not one this harness can show a model; "
            f"allowed: {', '.join(sorted(ALLOWED_MEDIA_TYPES))}"
        )
    try:
        raw = base64.b64decode(data_b64, validate=True)
    except Exception as exc:
        raise AttachmentError("data is not valid base64") from exc
    if not raw:
        raise AttachmentError("data is empty")
    if len(raw) > MAX_ATTACHMENT_BYTES:
        raise AttachmentError(
            f"attachment is {len(raw)} bytes; the limit is {MAX_ATTACHMENT_BYTES} "
            "(the request body limit is 1 MiB and base64 inflates by a third)"
        )
    if not raw.startswith(_MAGIC[media_type]):
        # The bytes decide, not the label. A caller may only be wrong about their own file
        # here; the value is that nothing else downstream has to wonder.
        raise AttachmentError(f"the bytes are not {media_type}")
    return raw


async def put_attachment(
    object_store: Any | None,
    *,
    tenant_id: str,
    data: bytes,
    media_type: str,
    filename: str = "",
) -> Attachment:
    """Store bytes under a fresh id and describe what was stored.

    The id is generated here rather than accepted from the caller. A caller-chosen id is a
    caller-chosen key, and the whole point of the containment check above is that no
    spelling of a reference reaches another tenant's data.
    """
    if object_store is None:
        raise AttachmentError("no object store is configured; set FELIX_OBJECT_STORE")
    # The tenant is half the key, and this is an exported API whose callers may one day not
    # be the HTTP doors. `read_artifact` checks the *real* tenant rather than a placeholder
    # and this module claimed parity with it; `valid_file_id` only ever proves things about
    # the id. Proven reachable in review with `tenant_id="../../manifests/acme"`.
    file_id = uuid.uuid4().hex
    key = attachment_key(tenant_id, file_id)
    if not _contained(tenant_id, key):
        raise AttachmentError("refusing to write outside the tenant prefix")
    await object_store.put(key, data, content_type=media_type)
    # Every interpolated value through `loggable`, which is the repo's rule and not a
    # judgement about which of these happens to be clean today. `tenant_id` is charset-bound
    # at the door and `file_id` is generated here, but `put_attachment` is an exported API:
    # a caller reaching it directly supplies both, plus a `media_type` that never met
    # `decode_upload`. A log line's separator is the newline, and "this one is validated
    # elsewhere" is the argument that has to be re-made every time the caller set changes.
    logger.info(
        "attachment stored tenant=%s file_id=%s media_type=%s bytes=%d",
        loggable(tenant_id, limit=64),
        loggable(file_id, limit=64),
        loggable(media_type, limit=64),
        len(data),
    )
    return Attachment(file_id=file_id, media_type=media_type, size_bytes=len(data), filename=filename)


async def read_attachment(object_store: Any | None, *, tenant_id: str, file_id: str) -> bytes | None:
    """The stored bytes, or None when the reference names nothing this tenant owns.

    None rather than an exception for a miss, so a route answers 404 for "no such file"
    and "not yours" alike — which reference ids exist is not a caller's business.
    """
    if object_store is None or not valid_file_id(file_id):
        return None
    key = attachment_key(tenant_id, file_id)
    if not _contained(tenant_id, key):
        return None
    try:
        return await object_store.get(key)
    except Exception:
        logger.warning("attachment read failed file_id=%s", loggable(file_id, limit=64), exc_info=True)
        return None


async def delete_attachment(object_store: Any | None, *, tenant_id: str, file_id: str) -> bool:
    """Remove an upload. True when a reference that could have named one was acted on.

    The erasure path. Everything stored here is caller-supplied by construction, so a
    surface that can only accumulate is one an operator cannot answer a deletion request
    with. Idempotent, and it does not distinguish "was not there" from "not yours" — the
    same reason the read does not.
    """
    if object_store is None or not valid_file_id(file_id):
        return False
    key = attachment_key(tenant_id, file_id)
    if not _contained(tenant_id, key):
        return False
    try:
        await object_store.delete(key)
    except Exception:
        logger.warning("attachment delete failed file_id=%s", loggable(file_id, limit=64), exc_info=True)
        return False
    logger.info(
        "attachment deleted tenant=%s file_id=%s",
        loggable(tenant_id, limit=64),
        loggable(file_id, limit=64),
    )
    return True


__all__ = [
    "ALLOWED_MEDIA_TYPES",
    "MAX_ATTACHMENT_BYTES",
    "MAX_MEDIA_TYPE_CHARS",
    "Attachment",
    "AttachmentError",
    "attachment_key",
    "decode_upload",
    "delete_attachment",
    "put_attachment",
    "read_attachment",
    "valid_file_id",
]
