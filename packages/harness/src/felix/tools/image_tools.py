"""Image tools: resize, crop, rotate, convert and thumbnail an image the model can see.

Bound from `spec.image_tools`, one op per tool, and backed by Pillow behind the `image` extra.
Without the extra each tool still binds and says what to install, which tells the model and
the operator more than a tool that silently is not there.

**Naming an image.** A model sees an image's pixels, not its file id, so `image` takes:

- `latest` (the default): the newest image in this thread;
- `#n`: the n-th image a `list` tool reported;
- `felix-file://<id>`: a reference a `list` tool or an earlier image tool reported.

Only images *in this thread* -- its active branch, as the model sees it -- can be named. A
reference to any other upload the tenant owns is refused: it would reach the model without
ever passing the screen a turn's images get. `path` reads a workspace file instead, and only
for a tool whose manifest entry sets `allow_path`.

**Screening.** A result is screened by where its input came from (`apply_content_screening`,
which reads `IMAGE_INPUT_KEY` on the output): with `image_model` set every result is screened
like an untrusted tool's image, and without it a result made from a workspace file is
quarantined. One made from a thread image passes -- no less screened than the image it came
from.

**Bounds.** Input is read up to `MAX_INPUT_BYTES`, must be png, jpeg, gif or webp by its bytes
(so no other Pillow parser, Ghostscript's EPS among them, is reachable), and is decoded only up to
`MAX_PIXELS`, checked before decoding. Output is at most `MAX_OUTPUT_SIDE` a side and is
downscaled until it fits the attachment limit. Pillow work runs off the event loop, at most
`MAX_CONCURRENT` at once per process.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import io
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from felix_ai.types import ImageAttachment, split_file_ref
from felix_ai.wire.base import split_data_url
from pydantic import BaseModel, ConfigDict, Field

from felix.context import current_tenant
from felix.manifests.schema import ImageToolRef
from felix.tools.errors import tool_error_output
from felix.tools.types import Tool, ToolInvocationCtx, ToolOutput, ToolOutputDict, define_tool

if TYPE_CHECKING:
    from PIL.Image import Image

MAX_INPUT_BYTES = 20 * 1024 * 1024
MAX_PIXELS = 4096 * 4096
MAX_OUTPUT_SIDE = 4096
MAX_LISTED = 50
MAX_CONCURRENT = 2

# Where a result's input came from, on the output's metadata, for content screening to read.
IMAGE_INPUT_KEY = "image_input"

_OPEN_FORMATS = ["PNG", "JPEG", "WEBP", "GIF"]
_FORMATS = {"png": "PNG", "jpeg": "JPEG", "webp": "WEBP", "gif": "GIF"}
_MEDIA = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp", "GIF": "image/gif"}

# Decoded images run to hundreds of megabytes; the default executor is shared by every
# `to_thread` in the process.
_PILLOW_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT)


class ImageToolError(ValueError):
    """Something the model asked for that cannot be done; the message is for the model."""


# --- arguments ------------------------------------------------------------------------------


class _Source(BaseModel):
    model_config = ConfigDict(extra="forbid")
    image: str = Field(
        default="latest",
        max_length=128,
        description="`latest`, `#n` from the list tool, or a `felix-file://` reference.",
    )
    path: str | None = Field(
        default=None, max_length=1024, description="A workspace file instead of `image`, where allowed."
    )


class ListArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InfoArgs(_Source):
    pass


class ResizeArgs(_Source):
    width: int | None = Field(default=None, ge=1, le=MAX_OUTPUT_SIDE)
    height: int | None = Field(default=None, ge=1, le=MAX_OUTPUT_SIDE)


class CropArgs(_Source):
    left: int = Field(ge=0)
    top: int = Field(ge=0)
    right: int = Field(ge=1)
    bottom: int = Field(ge=1)


class RotateArgs(_Source):
    degrees: float = Field(ge=-360, le=360, description="Counter-clockwise; the canvas grows to fit.")


class ConvertArgs(_Source):
    format: Literal["png", "jpeg", "webp", "gif"]
    quality: int = Field(default=85, ge=1, le=95, description="For jpeg and webp.")


class ThumbnailArgs(_Source):
    max_size: int = Field(default=512, ge=16, le=2048, description="Longest side, in pixels.")


# --- the transforms -------------------------------------------------------------------------


def _resize(img: Image, args: ResizeArgs) -> tuple[Image, str | None]:
    if args.width is None and args.height is None:
        raise ImageToolError("give a width, a height, or both")
    width = args.width or max(1, round(img.width * (args.height or 0) / img.height))
    height = args.height or max(1, round(img.height * (args.width or 0) / img.width))
    return img.resize(
        (min(width, MAX_OUTPUT_SIDE), min(height, MAX_OUTPUT_SIDE)), _pillow().Resampling.LANCZOS
    ), None


def _crop(img: Image, args: CropArgs) -> tuple[Image, str | None]:
    # Pillow pads a box outside the image rather than refusing it, so the bounds are ours.
    if not (args.left < args.right <= img.width and args.top < args.bottom <= img.height):
        raise ImageToolError(
            f"the box must lie inside the {img.width}x{img.height} image, left < right, top < bottom"
        )
    return img.crop((args.left, args.top, args.right, args.bottom)), None


def _rotate(img: Image, args: RotateArgs) -> tuple[Image, str | None]:
    return img.rotate(args.degrees, expand=True, resample=_pillow().Resampling.BICUBIC), None


def _convert(img: Image, args: ConvertArgs) -> tuple[Image, str | None]:
    return img, _FORMATS[args.format]


def _thumbnail(img: Image, args: ThumbnailArgs) -> tuple[Image, str | None]:
    thumb = img.copy()
    thumb.thumbnail((args.max_size, args.max_size), _pillow().Resampling.LANCZOS)
    return thumb, None


@dataclass(frozen=True)
class _Op:
    args: type[BaseModel]
    description: str
    # None for the ops that return text: `list` reads no image, `info` decodes but transforms nothing.
    transform: Callable[[Any, Any], tuple[Any, str | None]] | None = None


# The one table of ops. `ImageToolRef.op` is checked against its keys by a test, so an op added
# to the schema and not here fails there -- not as a KeyError the builder would swallow along
# with every other image tool in the manifest.
OPS: dict[str, _Op] = {
    "list": _Op(
        ListArgs, "List the images in this conversation, newest last, with the `#n` each can be named by."
    ),
    "info": _Op(InfoArgs, "Report an image's format, size and mode."),
    "resize": _Op(
        ResizeArgs, "Resize an image to a width and/or height (one alone keeps the aspect ratio).", _resize
    ),
    "crop": _Op(CropArgs, "Crop an image to the box left, top, right, bottom, in pixels.", _crop),
    "rotate": _Op(RotateArgs, "Rotate an image counter-clockwise by some degrees.", _rotate),
    "convert": _Op(ConvertArgs, "Re-encode an image as png, jpeg, webp or gif.", _convert),
    "thumbnail": _Op(
        ThumbnailArgs,
        "Shrink an image so its longest side is at most max_size, keeping the aspect ratio.",
        _thumbnail,
    ),
}


# --- finding an image -----------------------------------------------------------------------


async def _thread_images(thread_id: str | None) -> list[tuple[ImageAttachment, str]]:
    request = current_tenant()
    if not thread_id or request is None:
        return []
    from felix.session.store import get_session_store
    from felix.session.tree import branch_images

    settings, tenant = request
    return await branch_images(get_session_store(settings, tenant_id=tenant).open(thread_id))


async def _source_bytes(
    args: _Source, ctx: ToolInvocationCtx | None, *, allow_path: bool
) -> tuple[bytes, str]:
    """The input's bytes, and where they came from: `thread` or `workspace`."""
    if args.path:
        if not allow_path:
            raise ImageToolError("this tool reads images in the conversation, not workspace files")
        return await asyncio.to_thread(_read_workspace, args.path), "workspace"
    images = await _thread_images(ctx.thread_id if ctx else None)
    if not images:
        raise ImageToolError("there are no images in this conversation to work on")
    choice = args.image.strip()
    if choice in ("", "latest"):
        return await _read_url(images[-1][0].url), "thread"
    if choice.startswith("#") and choice[1:].isdigit():
        index = int(choice[1:])
        if 1 <= index <= len(images):
            return await _read_url(images[index - 1][0].url), "thread"
        raise ImageToolError(f"there is no image {choice}; this conversation has {len(images)}")
    if split_file_ref(choice):
        if choice not in {image.url for image, _ in images}:
            raise ImageToolError(f"{choice} is not an image in this conversation")
        return await _read_url(choice), "thread"
    raise ImageToolError("name an image as `latest`, `#n` from the list tool, or a felix-file:// reference")


async def _read_url(url: str) -> bytes:
    file_id = split_file_ref(url)
    if not file_id:
        # Decoded only: the type and size rules are `_open`'s, for every input alike.
        inline = split_data_url(url)
        if inline is None:
            raise ImageToolError("that image is not inline image data or a stored reference")
        try:
            return base64.b64decode(inline[1], validate=True)
        except binascii.Error as exc:
            raise ImageToolError("that image is not valid base64") from exc
    from felix.tools.tool_images import stored_image

    stored = await stored_image(file_id)
    if stored is None:
        raise ImageToolError(f"{url} can no longer be read")
    return stored[0]


def _read_workspace(user_path: str) -> bytes:
    from felix.tools.workspace import open_regular, open_workspace_parent, workspace_root

    with open_workspace_parent(workspace_root(), user_path) as (parent, leaf, rel):
        if leaf is None:
            raise ImageToolError(f"{rel} is a directory")
        fd = open_regular(parent, leaf, os.O_RDONLY, rel)
        try:
            if os.fstat(fd).st_size > MAX_INPUT_BYTES:
                raise ImageToolError(f"{rel} is over the {MAX_INPUT_BYTES}-byte limit for an input image")
            return os.read(fd, MAX_INPUT_BYTES)
        finally:
            os.close(fd)


# --- the work, off the event loop -----------------------------------------------------------


def _pillow() -> Any:
    from PIL import Image

    return Image


def _open(raw: bytes) -> Image:
    from felix.attachments import AttachmentError

    if len(raw) > MAX_INPUT_BYTES:
        raise ImageToolError(f"the image is over the {MAX_INPUT_BYTES}-byte limit for an input image")
    try:
        # The types an upload accepts, by the bytes: no other Pillow parser is reachable from
        # here. Not the upload's size limit -- an input may be larger than a result may be.
        _sniffed(raw)
    except AttachmentError as exc:
        raise ImageToolError(str(exc)) from exc
    image_mod = _pillow()
    try:
        img = image_mod.open(io.BytesIO(raw), formats=_OPEN_FORMATS)
    except image_mod.DecompressionBombError as exc:
        raise ImageToolError("the image decodes to more pixels than allowed") from exc
    except Exception as exc:
        raise ImageToolError("the data is not an image Pillow can read") from exc
    # Before decoding: `open` reads only the header. Our own check rather than Pillow's global
    # `MAX_IMAGE_PIXELS`, which is process-wide and would lower the limit for every other user.
    if img.width * img.height > MAX_PIXELS:
        raise ImageToolError(f"the image is {img.width}x{img.height}, over the {MAX_PIXELS}-pixel limit")
    try:
        img.load()
    except Exception as exc:
        raise ImageToolError("the data is not an image Pillow can read") from exc
    return img


def _attachment_limit() -> int:
    from felix.attachments import MAX_ATTACHMENT_BYTES

    return MAX_ATTACHMENT_BYTES


def _sniffed(raw: bytes) -> str:
    from felix.attachments import ALLOWED_MEDIA_TYPES, AttachmentError, sniff_media_type

    media_type = sniff_media_type(raw)
    if media_type is None:
        raise AttachmentError(f"the bytes are not one of {', '.join(sorted(ALLOWED_MEDIA_TYPES))}")
    return media_type


def _encode(img: Image, fmt: str, quality: int) -> bytes:
    """`img` encoded as `fmt`, downscaled until it fits the attachment limit."""
    image_mod = _pillow()
    if max(img.width, img.height) > MAX_OUTPUT_SIDE:
        img = img.copy()
        img.thumbnail((MAX_OUTPUT_SIDE, MAX_OUTPUT_SIDE), image_mod.Resampling.LANCZOS)
    limit = _attachment_limit()
    while True:
        frame = img
        if fmt == "JPEG" and frame.mode not in ("RGB", "L"):
            frame = frame.convert("RGB")
        buf = io.BytesIO()
        options: dict[str, Any] = {"quality": quality} if fmt in ("JPEG", "WEBP") else {}
        frame.save(buf, format=fmt, **options)
        data = buf.getvalue()
        if len(data) <= limit:
            return data
        if max(img.width, img.height) <= 64:
            raise ImageToolError("the result cannot be made small enough to show")
        img = img.resize(
            (max(1, img.width * 3 // 4), max(1, img.height * 3 // 4)), image_mod.Resampling.LANCZOS
        )


def _run(op: str, raw: bytes, args: BaseModel) -> tuple[str, bytes | None, str]:
    """`(summary, encoded result or None for info, media type)`."""
    with _PILLOW_SLOTS:
        img = _open(raw)
        source_format = (img.format or "PNG").upper()
        transform = OPS[op].transform
        if transform is None:
            animated = ", animated" if getattr(img, "is_animated", False) else ""
            return f"{source_format} {img.width}x{img.height}, mode {img.mode}{animated}", None, ""
        result, chosen = transform(img, args)
        fmt = chosen or (source_format if source_format in _MEDIA else "PNG")
        quality = args.quality if isinstance(args, ConvertArgs) else 85
        data = _encode(result, fmt, quality)
        return f"{fmt} {result.width}x{result.height}", data, _MEDIA[fmt]


# --- the tools ------------------------------------------------------------------------------


async def _list(ctx: ToolInvocationCtx | None) -> ToolOutput:
    images = await _thread_images(ctx.thread_id if ctx else None)
    if not images:
        return "No images in this conversation."
    first = max(0, len(images) - MAX_LISTED)
    return "\n".join(
        f"#{i + 1}: {image.url if split_file_ref(image.url) else '(inline)'} {origin}"
        for i, (image, origin) in enumerate(images)
        if i >= first
    )


def _handler(ref: ImageToolRef) -> Any:
    op = ref.op

    async def handler(args: Any, ctx: ToolInvocationCtx | None = None) -> ToolOutput:
        if op == "list":
            return await _list(ctx)
        try:
            _pillow()
        except ImportError:
            return tool_error_output(
                "transport_unavailable",
                "image_error: Pillow is not installed. Install felix-harness[image] (uv sync --extra image).",
            )
        from felix.tools.tool_images import store_image_bytes

        try:
            raw, origin = await _source_bytes(args, ctx, allow_path=ref.allow_path)
            summary, data, media_type = await asyncio.to_thread(_run, op, raw, args)
        except ImageToolError as exc:
            return tool_error_output("invalid_arguments", f"image_error: {exc}")
        except FileNotFoundError:
            return tool_error_output("invalid_arguments", f"image_error: no file at {args.path}")
        except ValueError as exc:  # the workspace's own refusals: escaping paths, no root
            return tool_error_output("invalid_arguments", f"image_error: {exc}")
        except OSError as exc:
            return tool_error_output("provider_error", f"image_error: {type(exc).__name__}")
        if data is None:
            return summary
        url, note = await store_image_bytes(data, media_type, tool_name=ref.name)
        if url is None:
            return f"{op} result: {summary}, {len(data)} bytes. {note}"
        shown = url if split_file_ref(url) else "(not stored: no request tenant)"
        return ToolOutputDict(
            content=f"{op} result: {summary}, {len(data)} bytes. Stored as {shown}.",
            metadata={IMAGE_INPUT_KEY: origin},
            attachments=[ImageAttachment(url=url, media_type=media_type)],
        )

    return handler


def tools_from_image_refs(refs: list[ImageToolRef]) -> list[Tool]:
    return [
        define_tool(
            name=ref.name,
            description=ref.description or OPS[ref.op].description,
            handler=_handler(ref),
            args=OPS[ref.op].args,
            source="image",
            fatal=ref.fatal,
        )
        for ref in refs
    ]


__all__ = [
    "IMAGE_INPUT_KEY",
    "MAX_CONCURRENT",
    "MAX_INPUT_BYTES",
    "MAX_OUTPUT_SIDE",
    "MAX_PIXELS",
    "OPS",
    "ImageToolError",
    "tools_from_image_refs",
]
