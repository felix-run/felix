"""A2A message parts: text, and images carried as FileParts.

Both directions used to keep the text parts and drop everything else without a word, so an
image sent to a Felix agent over A2A, or returned by a peer, simply vanished. A FilePart is

    {"kind": "file", "file": {"bytes": "<base64>", "mimeType": "image/png", "name": "x.png"}}

(`type` in place of `kind`, and `mime_type`, are accepted too: both spellings are in the wild).
Only the `bytes` form is accepted. The `uri` form would have the provider fetch something no
screen can read as sent -- the reason a remote image URL is refused everywhere else.

Inbound, a FilePart is held to the upload rules (`attachments.decode_upload`) and stored like an
upload; a part that fails them refuses the message rather than being dropped, so the sender
hears that the image was not seen. From a peer, it becomes a tool image, held to the same rules
before the screener reads it, with a note in the tool result for each it drops.
"""

from __future__ import annotations

import base64
import binascii
from typing import Any

from felix_ai.types import ImageAttachment
from felix_ai.wire.base import data_url


class PartError(ValueError):
    """A part this side cannot accept, with a message for the sender."""


def _is_file(part: dict[str, Any]) -> bool:
    return (part.get("kind") or part.get("type")) == "file"


def text_of(parts: list[Any]) -> str:
    texts = [
        str(p.get("text") or p.get("content") or "") for p in parts if isinstance(p, dict) and not _is_file(p)
    ]
    return "\n".join(t for t in texts if t)


def _file_fields(part: dict[str, Any]) -> tuple[str, str, str, str]:
    """`(bytes, uri, media type, name)` of a FilePart, each `""` when absent."""
    raw = part.get("file")
    file: dict[str, Any] = raw if isinstance(raw, dict) else {}

    def field(*keys: str) -> str:
        value = next((file[k] for k in keys if file.get(k)), "")
        return value if isinstance(value, str) else ""

    return field("bytes"), field("uri"), field("mimeType", "mime_type"), field("name")


def inbound_images(parts: list[Any]) -> list[tuple[bytes, str]]:
    """`(bytes, media type)` for each image a sender's FileParts carry, or `PartError`.

    Held to the upload rules (`attachments.decode_upload`): an allowed type, decided by the
    bytes, within the size limit. A missing `mimeType` -- optional in A2A -- is read from the
    bytes rather than refused. The server stores what this returns; nothing here does.
    """
    from felix.attachments import AttachmentError, decode_upload, sniff_media_type

    images: list[tuple[bytes, str]] = []
    for part in parts:
        if not isinstance(part, dict) or not _is_file(part):
            continue
        data, uri, media, name = _file_fields(part)
        if uri and not data:
            raise PartError("file parts must carry their bytes; a file by uri is not fetched")
        label = f" {name!r}" if name else ""
        try:
            if not media:
                media = sniff_media_type(base64.b64decode(data, validate=True)) or ""
            images.append((decode_upload(data, media), media))
        except binascii.Error as exc:
            raise PartError(f"file part{label}: data is not valid base64") from exc
        except AttachmentError as exc:
            raise PartError(f"file part{label}: {exc}") from exc
    return images


def returned_images(parts: list[Any], *, tool_name: str) -> tuple[list[ImageAttachment], list[str]]:
    """A peer's FileParts as tool images, checked before anything reads them, and a note for
    each that cannot be shown (`tool_images.screenable_tool_images`)."""
    from felix.tools.tool_images import screenable_tool_images

    images: list[ImageAttachment] = []
    notes: list[str] = []
    for part in parts:
        if not isinstance(part, dict) or not _is_file(part):
            continue
        data, uri, _media, _name = _file_fields(part)
        if data:
            # The label is not trusted: the check reads the type from the bytes.
            images.append(ImageAttachment(url=data_url("application/octet-stream", data)))
        elif uri:
            notes.append("[file dropped: the peer returned a file by uri, which is not fetched]")
    kept, dropped = screenable_tool_images(images, tool_name=tool_name)
    return kept, [*notes, *dropped]


__all__ = ["PartError", "inbound_images", "returned_images", "text_of"]
