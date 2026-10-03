"""The Pillow image tools: each op on a real image, the bounds, and where an image may come from."""

from __future__ import annotations

import base64
import io
import os
from pathlib import Path
from typing import Any, get_args

import pytest
from felix.config import Settings
from felix.manifests.schema import ContentScreening, ImageToolRef
from felix.tools.types import ToolInvocationCtx, tool_output_content, tool_output_images

from tests.optional_deps import require_optional


@pytest.fixture
def pil() -> Any:
    return require_optional("PIL.Image", "image")


def _encoded(pil: Any, img: Any, fmt: str = "PNG", **options: Any) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format=fmt, **options)
    return buf.getvalue()


def _png(pil: Any, size: tuple[int, int] = (200, 100), mode: str = "RGB", noise: bool = False) -> bytes:
    if noise:
        img = pil.frombytes(mode, size, os.urandom(size[0] * size[1] * len(mode)))
    else:
        img = pil.new(mode, size, "red" if mode == "RGB" else 0)
    return _encoded(pil, img)


def _data_url(raw: bytes, media: str = "image/png") -> str:
    return f"data:{media};base64,{base64.b64encode(raw).decode()}"


def _tool(op: str, *, allow_path: bool = False) -> Any:
    from felix.tools.image_tools import tools_from_image_refs

    (tool,) = tools_from_image_refs([ImageToolRef(name=f"img_{op}", op=op, allow_path=allow_path)])  # type: ignore[arg-type]
    return tool


def _ctx(tmp_path: Path | None = None, tenant: str = "acme") -> Any:
    from felix.context import AuthContext, RequestContext

    settings = Settings(object_store="memory", workspace_root=str(tmp_path) if tmp_path else "")
    return RequestContext(settings=settings, auth=AuthContext(tenant_id=tenant), thread_id="acme:pics")


async def _run(op: str, args: dict[str, Any], *, ctx: Any = None, allow_path: bool = False) -> Any:
    from felix.context import async_run_with_context

    tool = _tool(op, allow_path=allow_path)
    async with async_run_with_context(ctx or _ctx()):
        return await tool.executor.execute(args, ToolInvocationCtx(thread_id="acme:pics"))


async def _append(*messages: Any) -> None:
    from felix.session.store import get_session_store
    from felix.session.types import chat_message_to_event

    session = get_session_store(_ctx().settings, tenant_id="acme").open("acme:pics")
    for m in messages:
        await session.append(chat_message_to_event(m))


async def _seed_thread(*urls: str) -> None:
    """A thread whose log holds these images on user turns, the way an upload lands."""
    from felix_ai.types import ChatMessage, ImageAttachment

    await _append(
        *(ChatMessage(role="user", content="look", attachments=[ImageAttachment(url=u)]) for u in urls)
    )


async def _as_tool_result(out: Any, name: str = "img_thumbnail") -> str:
    """Put a tool's result in the log the way the runner does, and return its reference."""
    from felix_ai.types import ChatMessage

    (att,) = tool_output_images(out)
    await _append(ChatMessage(role="tool", name=name, tool_call_id="c", content="", attachments=[att]))
    return att.url


async def _result_image(pil: Any, out: Any) -> tuple[Any, bytes]:
    """The image a tool returned, read back from the store the way the wire would."""
    from felix.attachments import read_attachment
    from felix.storage import get_object_store
    from felix_ai.types import split_file_ref

    (att,) = tool_output_images(out)
    file_id = split_file_ref(att.url)
    assert file_id, f"the result should be stored, not inline: {att.url[:40]}"
    raw = await read_attachment(
        get_object_store(Settings(object_store="memory")), tenant_id="acme", file_id=file_id
    )
    assert raw is not None
    assert f"Stored as {att.url}" in tool_output_content(out), "the reply names the reference to chain on"
    return pil.open(io.BytesIO(raw)), raw


# --- the op table --------------------------------------------------------------------------


def test_every_op_the_schema_allows_is_in_the_table() -> None:
    """Missing from the table, an op is a KeyError the builder swallows with every image tool."""
    from felix.tools.image_tools import OPS

    assert set(get_args(ImageToolRef.model_fields["op"].annotation)) == set(OPS)


def test_each_op_binds_a_tool_with_its_own_arguments() -> None:
    from felix.tools.image_tools import OPS, tools_from_image_refs

    tools = tools_from_image_refs([ImageToolRef(name=f"t_{op}", op=op) for op in OPS])  # type: ignore[arg-type]
    by_name = {t.name: t for t in tools}
    assert "left" in by_name["t_crop"].args_schema.model_fields  # type: ignore[union-attr]
    assert "degrees" in by_name["t_rotate"].args_schema.model_fields  # type: ignore[union-attr]
    assert {t.source for t in tools} == {"image"}


# --- each op on a real image ---------------------------------------------------------------


@pytest.mark.parametrize(("args", "size"), [({"width": 50}, (50, 25)), ({"height": 50}, (100, 50))])
async def test_resize_with_one_side_keeps_the_aspect_ratio(
    pil: Any, args: dict[str, int], size: tuple[int, int]
) -> None:
    await _seed_thread(_data_url(_png(pil, (200, 100))))
    img, _ = await _result_image(pil, await _run("resize", args))
    assert img.size == size


async def test_resize_needs_a_side(pil: Any) -> None:
    await _seed_thread(_data_url(_png(pil)))
    assert "give a width, a height, or both" in tool_output_content(await _run("resize", {}))


async def test_crop_takes_the_box(pil: Any) -> None:
    await _seed_thread(_data_url(_png(pil, (200, 100))))
    img, _ = await _result_image(pil, await _run("crop", {"left": 10, "top": 20, "right": 60, "bottom": 50}))
    assert img.size == (50, 30)


@pytest.mark.parametrize(
    "box",
    [
        {"left": 0, "top": 0, "right": 300, "bottom": 50},
        {"left": 0, "top": 0, "right": 50, "bottom": 150},
        {"left": 60, "top": 0, "right": 50, "bottom": 50},
        {"left": 0, "top": 60, "right": 50, "bottom": 50},
    ],
    ids=["right-outside", "bottom-outside", "left-past-right", "top-past-bottom"],
)
async def test_a_crop_box_outside_the_image_is_refused(pil: Any, box: dict[str, int]) -> None:
    """Pillow pads a box outside the image instead of refusing it, so each bound is ours."""
    await _seed_thread(_data_url(_png(pil, (200, 100))))
    out = await _run("crop", box)
    assert "must lie inside the 200x100 image" in tool_output_content(out)
    assert tool_output_images(out) == []


async def test_rotate_grows_the_canvas(pil: Any) -> None:
    await _seed_thread(_data_url(_png(pil, (200, 100))))
    img, _ = await _result_image(pil, await _run("rotate", {"degrees": 90}))
    assert img.size == (100, 200)


async def test_a_result_past_the_side_limit_is_clamped(pil: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import felix.tools.image_tools as mod

    monkeypatch.setattr(mod, "MAX_OUTPUT_SIDE", 100)
    await _seed_thread(_data_url(_png(pil, (200, 100))))
    img, _ = await _result_image(pil, await _run("rotate", {"degrees": 90}))
    assert img.size == (50, 100)


async def test_convert_changes_the_format_and_media_type(pil: Any) -> None:
    await _seed_thread(_data_url(_png(pil, (40, 40), mode="RGBA")))
    out = await _run("convert", {"format": "jpeg", "quality": 70})
    assert tool_output_images(out)[0].media_type == "image/jpeg"
    img, _ = await _result_image(pil, out)
    assert img.format == "JPEG"


async def test_thumbnail_bounds_the_longest_side(pil: Any) -> None:
    await _seed_thread(_data_url(_png(pil, (400, 100))))
    img, _ = await _result_image(pil, await _run("thumbnail", {"max_size": 64}))
    assert img.size == (64, 16)


async def test_info_describes_without_returning_an_image(pil: Any) -> None:
    await _seed_thread(_data_url(_png(pil, (200, 100))))
    assert await _run("info", {}) == "PNG 200x100, mode RGB"


async def test_info_says_an_image_is_animated(pil: Any) -> None:
    frames = [pil.new("RGB", (20, 20), c) for c in ("red", "blue")]
    gif = _encoded(pil, frames[0], "GIF", save_all=True, append_images=frames[1:])
    await _seed_thread(_data_url(gif, "image/gif"))
    assert (await _run("info", {})).endswith(", animated")


# --- naming an image -----------------------------------------------------------------------


async def test_latest_is_the_newest_image_and_hash_n_counts_from_the_oldest(pil: Any) -> None:
    await _seed_thread(_data_url(_png(pil, (10, 10))), _data_url(_png(pil, (20, 20))))
    assert await _run("info", {}) == "PNG 20x20, mode RGB"
    assert await _run("info", {"image": "#1"}) == "PNG 10x10, mode RGB"
    for missing in ("#0", "#3"):
        assert f"no image {missing}" in tool_output_content(await _run("info", {"image": missing}))


async def test_a_result_in_the_thread_can_be_listed_and_chained(pil: Any) -> None:
    await _seed_thread(_data_url(_png(pil, (200, 100))))
    ref = await _as_tool_result(await _run("thumbnail", {"max_size": 50}))
    assert await _run("info", {"image": ref}) == "PNG 50x25, mode RGB", "a result is an input"
    assert await _run("info", {}) == "PNG 50x25, mode RGB", "and is now the latest"
    assert (await _run("list", {})).splitlines() == [
        "#1: (inline) from the user",
        f"#2: {ref} returned by img_thumbnail",
    ]


async def test_list_shows_the_newest_images_numbered_as_they_are_named(pil: Any) -> None:
    from felix.tools.image_tools import MAX_LISTED

    await _seed_thread(*[_data_url(_png(pil, (4, 4)))] * (MAX_LISTED + 2))
    lines = (await _run("list", {})).splitlines()
    assert len(lines) == MAX_LISTED and lines[0].startswith("#3: ")


async def test_a_reference_outside_the_thread_is_refused(pil: Any) -> None:
    """An upload the tenant owns but this thread never showed would pass every screen."""
    await _seed_thread(_data_url(_png(pil, (20, 20))))
    stray = tool_output_images(await _run("thumbnail", {"max_size": 16}))[0].url  # never put in the log
    assert f"{stray} is not an image in this conversation" in tool_output_content(
        await _run("info", {"image": stray})
    )


async def test_a_remote_url_and_an_empty_thread_are_refused(pil: Any) -> None:
    assert "no images in this conversation" in tool_output_content(await _run("info", {}))
    await _seed_thread("https://example.com/x.png")
    assert "not inline image data or a stored reference" in tool_output_content(await _run("info", {}))


async def test_another_tenants_thread_shows_none_of_this_ones_images(pil: Any) -> None:
    await _seed_thread(_data_url(_png(pil, (20, 20))))
    out = await _run("info", {}, ctx=_ctx(tenant="globex"))
    assert "no images in this conversation" in tool_output_content(out)


# --- workspace files -----------------------------------------------------------------------


async def test_a_workspace_file_is_read_only_where_the_manifest_allows_it(pil: Any, tmp_path: Path) -> None:
    (tmp_path / "pic.png").write_bytes(_png(pil, (30, 60)))
    ctx = _ctx(tmp_path)
    assert await _run("info", {"path": "pic.png"}, ctx=ctx, allow_path=True) == "PNG 30x60, mode RGB"
    refused = await _run("info", {"path": "pic.png"}, ctx=ctx)
    assert "not workspace files" in tool_output_content(refused)


@pytest.mark.parametrize(
    ("path", "reason"),
    [("../outside.png", "escapes"), ("/etc/outside.png", "absolute")],
    ids=["escaping", "absolute"],
)
async def test_a_path_outside_the_workspace_is_refused(
    pil: Any, tmp_path: Path, path: str, reason: str
) -> None:
    """A real image waits outside the root, so only the guard can make this fail."""
    root = tmp_path / "ws"
    root.mkdir()
    (tmp_path / "outside.png").write_bytes(_png(pil, (30, 60)))
    out = await _run("info", {"path": path}, ctx=_ctx(root), allow_path=True)
    assert reason in tool_output_content(out) and "PNG" not in tool_output_content(out)


async def test_a_missing_or_oversized_workspace_file_is_refused(
    pil: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import felix.tools.image_tools as mod

    ctx = _ctx(tmp_path)
    missing = await _run("info", {"path": "nope.png"}, ctx=ctx, allow_path=True)
    assert "no file at nope.png" in tool_output_content(missing)
    (tmp_path / "big.png").write_bytes(_png(pil, (200, 100), noise=True))
    monkeypatch.setattr(mod, "MAX_INPUT_BYTES", 100)
    big = await _run("info", {"path": "big.png"}, ctx=ctx, allow_path=True)
    assert "big.png is over the 100-byte limit" in tool_output_content(big)


async def test_an_unreadable_workspace_file_reports_only_the_error_type(pil: Any, tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root reads a 000 file")
    locked = tmp_path / "locked.png"
    locked.write_bytes(_png(pil))
    locked.chmod(0)
    try:
        out = await _run("info", {"path": "locked.png"}, ctx=_ctx(tmp_path), allow_path=True)
    finally:
        locked.chmod(0o600)
    assert tool_output_content(out).endswith("image_error: PermissionError")


# --- what Pillow is allowed to parse, and how much ------------------------------------------


@pytest.mark.parametrize("fmt", ["BMP", "TIFF", "EPS-header"], ids=["bmp", "tiff", "eps"])
async def test_only_the_upload_types_are_parsed(pil: Any, tmp_path: Path, fmt: str) -> None:
    """Every other Pillow parser is attack surface, EPS's Ghostscript most of all."""
    raw = (
        b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 10 10\n"
        if fmt == "EPS-header"
        else _encoded(pil, pil.new("RGB", (10, 10)), fmt)
    )
    (tmp_path / "in.bin").write_bytes(raw)
    out = await _run("info", {"path": "in.bin"}, ctx=_ctx(tmp_path), allow_path=True)
    assert "the bytes are not one of" in tool_output_content(out)


async def test_an_image_whose_header_claims_too_many_pixels_is_refused_before_decoding(pil: Any) -> None:
    """A few kilobytes of PNG can claim gigabytes of pixels. Checked by us, not by Pillow's
    process-wide `MAX_IMAGE_PIXELS`, which this module no longer touches."""
    from felix.tools.image_tools import MAX_PIXELS

    before = pil.MAX_IMAGE_PIXELS
    side = int(MAX_PIXELS**0.5) + 1
    await _seed_thread(_data_url(_png(pil, (side, side), mode="1")))
    out = await _run("thumbnail", {"max_size": 64})
    assert f"over the {MAX_PIXELS}-pixel limit" in tool_output_content(out)
    assert tool_output_images(out) == []
    assert before == pil.MAX_IMAGE_PIXELS


async def test_an_input_over_the_byte_limit_is_refused(pil: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import felix.tools.image_tools as mod

    monkeypatch.setattr(mod, "MAX_INPUT_BYTES", 100)
    await _seed_thread(_data_url(_png(pil, (200, 100), noise=True)))
    assert "byte limit for an input image" in tool_output_content(await _run("info", {}))


async def test_a_result_is_downscaled_until_it_fits_the_attachment_limit(
    pil: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import felix.attachments as attachments

    monkeypatch.setattr(attachments, "MAX_ATTACHMENT_BYTES", 40_000)
    await _seed_thread(_data_url(_png(pil, (300, 300), noise=True)))
    img, raw = await _result_image(pil, await _run("rotate", {"degrees": 0}))
    assert len(raw) <= 40_000 and img.size[0] < 300


async def test_a_result_that_cannot_fit_is_refused_rather_than_looping(
    pil: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import felix.attachments as attachments

    monkeypatch.setattr(attachments, "MAX_ATTACHMENT_BYTES", 50)
    await _seed_thread(_data_url(_png(pil, (300, 300), noise=True)))
    assert "cannot be made small enough" in tool_output_content(await _run("rotate", {"degrees": 0}))


async def test_not_an_image_is_said_so(pil: Any) -> None:
    await _seed_thread(_data_url(b"plain text, not a picture"))
    assert "the bytes are not one of" in tool_output_content(await _run("info", {}))


async def test_without_pillow_the_tool_says_what_to_install_and_list_still_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    monkeypatch.setitem(sys.modules, "PIL", None)
    monkeypatch.setitem(sys.modules, "PIL.Image", None)
    out = await _run("resize", {"width": 10})
    assert "Install felix-harness[image]" in tool_output_content(out)
    assert out.metadata.get("error_code") == "transport_unavailable"
    assert await _run("list", {}) == "No images in this conversation."


# --- the budget and the store --------------------------------------------------------------


async def test_a_spent_budget_stores_nothing(pil: Any) -> None:
    """Drawn before the write: once the run's images are spent, no quota is charged either."""
    from felix.attachments import tenant_attachment_bytes
    from felix.context import async_run_with_context
    from felix.tools.tool_images import ImageBudget, request_budget

    await _seed_thread(_data_url(_png(pil, (20, 20))))
    ctx = _ctx()
    ctx.extras["tool_image_budget"] = ImageBudget(remaining=0)
    async with async_run_with_context(ctx):
        before = await tenant_attachment_bytes(ctx.settings, "acme")
        out = await _tool("thumbnail").executor.execute(
            {"max_size": 16}, ToolInvocationCtx(thread_id="acme:pics")
        )
        assert await tenant_attachment_bytes(ctx.settings, "acme") == before
        assert request_budget().remaining == 0
    assert "per run limit" in tool_output_content(out) and tool_output_images(out) == []


async def test_a_refused_store_does_not_show_the_tenants_totals(pil: Any) -> None:
    await _seed_thread(_data_url(_png(pil, (20, 20))))
    ctx = _ctx()
    ctx.settings.attachments_max_bytes_per_tenant = 8
    out = await _run("thumbnail", {"max_size": 16}, ctx=ctx)
    assert "could not be stored" in tool_output_content(out)
    assert "ceiling" not in tool_output_content(out) and "FELIX_" not in tool_output_content(out)


async def test_the_runner_does_not_charge_an_image_tools_result_twice(pil: Any) -> None:
    from felix.context import async_run_with_context
    from felix.tools.tool_images import ImageBudget, request_budget, store_tool_images

    await _seed_thread(_data_url(_png(pil, (20, 20))))
    ctx = _ctx()
    ctx.extras["tool_image_budget"] = ImageBudget(remaining=2)
    async with async_run_with_context(ctx):
        out = await _tool("thumbnail").executor.execute(
            {"max_size": 16}, ToolInvocationCtx(thread_id="acme:pics")
        )
        kept, _ = await store_tool_images(
            tool_output_images(out), tool_name="img_thumbnail", budget=ImageBudget()
        )
        assert len(kept) == 1 and request_budget().remaining == 1


# --- screening, by where the input came from -----------------------------------------------


async def _screened(
    op: str, args: dict[str, Any], *, images: Any = None, tmp_path: Path | None = None
) -> Any:
    from felix.context import async_run_with_context
    from felix.manifests.builder import apply_content_screening

    (tool,) = apply_content_screening(
        [_tool(op, allow_path=True)], ContentScreening(enabled=True), "m", images=images
    )
    async with async_run_with_context(_ctx(tmp_path)):
        return await tool.executor.execute(args, ToolInvocationCtx(thread_id="acme:pics"))


async def test_without_image_model_a_workspace_result_is_quarantined(pil: Any, tmp_path: Path) -> None:
    from felix.manifests.builder import TOOL_IMAGE_UNSCREENED

    (tmp_path / "pic.png").write_bytes(_png(pil, (30, 60)))
    out = await _screened("thumbnail", {"path": "pic.png", "max_size": 16}, tmp_path=tmp_path)
    assert tool_output_images(out) == [] and TOOL_IMAGE_UNSCREENED in tool_output_content(out)


async def test_without_image_model_a_thread_result_passes(pil: Any) -> None:
    await _seed_thread(_data_url(_png(pil, (30, 60))))
    out = await _screened("thumbnail", {"max_size": 16})
    assert len(tool_output_images(out)) == 1


async def test_with_image_model_every_result_is_screened(pil: Any) -> None:
    from felix_ai.types import ChatMessage

    seen: list[Any] = []

    class Refuses:
        async def screen(self, msg: ChatMessage) -> ChatMessage:
            seen.append(msg)
            return ChatMessage(
                role=msg.role, content=f"{msg.content}\n[quarantined] image flagged", attachments=None
            )

    await _seed_thread(_data_url(_png(pil, (30, 60))))
    out = await _screened("thumbnail", {"max_size": 16}, images=lambda: Refuses())
    assert len(seen) == 1 and tool_output_images(out) == []
    assert "[quarantined] image flagged" in tool_output_content(out)


async def test_after_a_rewind_latest_is_the_active_branchs_image(pil: Any) -> None:
    """An abandoned branch's image is one the model no longer sees; it must not be `latest`."""
    from felix.session.store import get_session_store
    from felix.session.tree import annotate_and_append, rewind_to
    from felix.session.types import chat_message_to_event
    from felix_ai.types import ChatMessage, ImageAttachment

    session = get_session_store(_ctx().settings, tenant_id="acme").open("acme:pics")

    def turn(size: tuple[int, int]) -> Any:
        url = _data_url(_png(pil, size))
        return chat_message_to_event(
            ChatMessage(role="user", content="look", attachments=[ImageAttachment(url=url)])
        )

    (kept,) = await annotate_and_append(session, [turn((10, 10))])
    await annotate_and_append(session, [turn((99, 99))])
    assert (await rewind_to(session, kept))["ok"]
    assert await _run("info", {}) == "PNG 10x10, mode RGB"
    assert (await _run("list", {})).splitlines() == ["#1: (inline) from the user"]
