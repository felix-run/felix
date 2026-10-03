"""What happens to the images a tool returns, between the governance stack and the session log.

The tool layer's policy, kept out of `attachments.py`, which holds the storage primitives this
calls (`image_media_type`, `put_attachment`) and takes its tenant and store as arguments.
Here the tenant comes from the request, because a tool result has no other owner.

Three rules, each with a note in the tool result when it drops something, so the model knows
an image it was promised is missing:

- **Inline bytes only.** A tool's image is a `data:` URL, or a `felix-file://` reference that
  already resolves under the request's tenant. A remote URL would have the provider fetch
  something nothing here screened, and is dropped.
- **Upload rules.** The same size limit and allowed types as `POST /files`, decided by the
  bytes (`attachments.image_media_type`).
- **Bounded.** At most `MAX_IMAGES_PER_CALL` per call and `MAX_IMAGES_PER_RUN` per run. Tool
  images share the tenant's attachment quota with uploads, so a page that talks an agent into
  screenshotting in a loop would otherwise fill it and refuse every user's upload.

Stored images go to the attachment store under the request's tenant -- the same quota and
retention as an upload -- and the session log keeps a reference, not the base64, which would
otherwise ride along in every replay, compaction checkpoint and export of the thread.
"""

from __future__ import annotations

import base64
import binascii
import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace

from felix_ai.types import ImageAttachment, file_ref_url, split_file_ref
from felix_ai.wire.base import split_data_url

from felix.logging_setup import loggable

logger = logging.getLogger("felix.tools.images")

MAX_IMAGES_PER_CALL = 4
MAX_IMAGES_PER_RUN = 16


@dataclass
class ImageBudget:
    """How many more tool images this run may keep.

    One per request, held in the request context, so every agent the request runs -- a
    router's children, a delegating pattern's sub-agents -- draws from the same sixteen. The
    `ToolRunner`'s own is only the fallback for a run with no request context.
    """

    remaining: int = MAX_IMAGES_PER_RUN


_BUDGET_KEY = "tool_image_budget"


async def store_tool_images(
    images: Sequence[ImageAttachment], *, tool_name: str, budget: ImageBudget
) -> tuple[list[ImageAttachment], list[str]]:
    """A tool's images as stored references, and a note for each one that was not kept.

    With no request tenant -- an eval run -- there is no one to store an image for, and it
    stays inline. It is held to every other rule all the same. Governed tools never get
    here without one: `apply_limits` refuses a call outside a request.
    """
    from felix.context import try_get_context

    ctx = try_get_context()
    if ctx is not None:
        budget = ctx.extras.setdefault(_BUDGET_KEY, budget)
    tenant_id = getattr(getattr(ctx, "auth", None), "tenant_id", None) if ctx else None
    settings = getattr(ctx, "settings", None) if ctx else None
    name = loggable(tool_name, limit=64)
    kept: list[ImageAttachment] = []
    notes: list[str] = []
    for index, image in enumerate(images):
        if index >= MAX_IMAGES_PER_CALL or budget.remaining <= 0:
            limit = "per call" if index >= MAX_IMAGES_PER_CALL else "per run"
            notes.append(f"[image dropped: {name} returned more images than the {limit} limit]")
            continue
        if split_file_ref(image.url):
            budget.remaining -= 1
            kept.append(replace(image, filename=None))
            continue
        raw, media_type, problem = _decoded(image.url)
        if problem:
            notes.append(f"[image dropped: {name} returned an image that {problem}]")
            continue
        assert raw is not None and media_type is not None
        budget.remaining -= 1
        if not tenant_id:
            kept.append(replace(image, media_type=media_type, filename=None))
            continue
        # No filename: a tool's own label would reach the ledger and the log unmasked.
        stored = await _put(raw, media_type, tenant_id=str(tenant_id), settings=settings)
        if stored is None:
            # Dropped, not kept inline: inline would put the bytes the store just refused into
            # the log anyway. The reason stays in the server log -- a quota message carries the
            # tenant's totals, which the model, and whoever is injecting it, need not see.
            notes.append(f"[image dropped: {name} returned an image that could not be stored]")
            continue
        kept.append(replace(image, url=file_ref_url(stored), media_type=media_type, filename=None))
    return kept, notes


def _decoded(url: str) -> tuple[bytes | None, str | None, str]:
    """`(bytes, media type, "")` for an image a caller could have uploaded, else a reason."""
    from felix.attachments import AttachmentError, image_media_type

    inline = split_data_url(url)
    if inline is None:
        return None, None, "is not inline image data"
    try:
        raw = base64.b64decode(inline[1], validate=True)
        return raw, image_media_type(raw), ""
    except binascii.Error:
        return None, None, "is not valid base64"
    except AttachmentError as exc:
        return None, None, str(exc)


async def _put(raw: bytes, media_type: str, *, tenant_id: str, settings: object | None) -> str | None:
    from felix.attachments import put_attachment
    from felix.config import Settings, get_settings
    from felix.storage import get_object_store

    bound = settings if isinstance(settings, Settings) else get_settings()
    try:
        stored = await put_attachment(
            get_object_store(bound),
            tenant_id=tenant_id,
            data=raw,
            media_type=media_type,
            filename="",
            settings=bound,
        )
    except Exception as exc:
        logger.warning("tool image not stored for tenant %s: %s", loggable(tenant_id, limit=64), exc)
        return None
    return stored.file_id


__all__ = ["MAX_IMAGES_PER_CALL", "MAX_IMAGES_PER_RUN", "ImageBudget", "store_tool_images"]
