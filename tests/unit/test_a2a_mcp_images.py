"""Images over A2A and MCP: FileParts and image content blocks, both directions, wired."""

from __future__ import annotations

import base64
from typing import Any

import pytest
from felix.config import Settings
from felix.tools.types import ToolInvocationCtx, ToolOutputDict, tool_output_content, tool_output_images
from felix_ai.types import ImageAttachment

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
PNG_B64 = base64.b64encode(PNG_BYTES).decode()
PNG = f"data:image/png;base64,{PNG_B64}"
NOT_AN_IMAGE_B64 = base64.b64encode(b"<html>hi</html>").decode()


def _file_part(data: str = PNG_B64, media: str | None = "image/png", **file: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"bytes": data, **file}
    if media is not None:
        body["mimeType"] = media
    return {"kind": "file", "file": body}


# --- A2A parts -----------------------------------------------------------------------------


def test_an_inbound_file_part_becomes_image_bytes_and_text_parts_stay_text() -> None:
    from felix.a2a.parts import inbound_images, text_of

    parts = [{"kind": "text", "text": "what is this?"}, _file_part()]
    assert text_of(parts) == "what is this?"
    assert inbound_images(parts) == [(PNG_BYTES, "image/png")]


@pytest.mark.parametrize(
    "part",
    [
        {"type": "file", "file": {"bytes": PNG_B64, "mime_type": "image/png"}},
        _file_part(media=None),
    ],
    ids=["older-spellings", "no-mimeType"],
)
def test_older_spellings_and_a_missing_mime_type_are_accepted(part: dict[str, Any]) -> None:
    """`mimeType` is optional in A2A; the type is read from the bytes instead."""
    from felix.a2a.parts import inbound_images

    assert inbound_images([part]) == [(PNG_BYTES, "image/png")]


@pytest.mark.parametrize(
    ("part", "reason"),
    [
        ({"kind": "file", "file": {"uri": "https://example.com/x.png", "mimeType": "image/png"}}, "uri"),
        (_file_part(base64.b64encode(b"%PDF-1.7").decode(), "application/pdf"), "application/pdf"),
        (_file_part(NOT_AN_IMAGE_B64), "image/png"),
        (_file_part("***"), "base64"),
    ],
    ids=["uri", "pdf", "bad-magic", "not-base64"],
)
def test_an_inbound_file_part_that_fails_the_upload_rules_is_refused(
    part: dict[str, Any], reason: str
) -> None:
    from felix.a2a.parts import PartError, inbound_images

    with pytest.raises(PartError, match=reason):
        inbound_images([part])


def test_a_peers_file_parts_are_checked_before_anything_reads_them() -> None:
    """The screener runs before the runner: unchecked, it would transcribe anything sent."""
    from felix.a2a.parts import returned_images

    parts = [
        _file_part(media="text/plain"),  # a lying label: the bytes decide
        _file_part(NOT_AN_IMAGE_B64),
        {"kind": "file", "file": {"uri": "https://x/y.png"}},
    ]
    images, notes = returned_images(parts, tool_name="peer__p")
    assert [(a.url, a.media_type) for a in images] == [(PNG, "image/png")]
    assert len(notes) == 2 and any("by uri" in n for n in notes) and any("peer__p" in n for n in notes)


# --- A2A peers -----------------------------------------------------------------------------


def test_a_felix_peers_answer_is_read_from_its_artifacts() -> None:
    """A Felix peer answers in `artifacts`, with no status message: that read as a dict repr."""
    from felix.a2a.peers import _peer_output

    body = {
        "result": {
            "status": {"state": "completed"},
            "artifacts": [{"parts": [{"type": "text", "text": "it is 4"}]}],
        }
    }
    assert _peer_output(body) == "it is 4"


def test_an_answer_in_artifacts_wins_over_a_status_line_and_is_said_once() -> None:
    from felix.a2a.peers import _peer_output

    status = {"message": {"parts": [{"type": "text", "text": "working on it"}]}}
    artifacts = [{"parts": [{"type": "text", "text": "it is 4"}]}]
    assert _peer_output({"result": {"status": status, "artifacts": artifacts}}) == "it is 4"
    assert _peer_output({"result": {"status": status}}) == "working on it"


def _stub_http(monkeypatch: pytest.MonkeyPatch, module: Any, answer: Any) -> None:
    class _Resp:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> Any:
            return answer

    class _Client:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *a: Any) -> bool:
            return False

        async def post(self, url: str, json: Any = None, headers: Any = None) -> _Resp:
            return _Resp()

    monkeypatch.setattr(module, "safe_async_client", lambda **k: _Client())


async def test_a_peers_image_reaches_the_runner_and_is_quarantined_without_image_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The handler's wiring, then the governance it inherits as an untrusted `a2a` tool."""
    import felix.a2a.peers as peers
    from felix.manifests.builder import TOOL_IMAGE_UNSCREENED, apply_content_screening
    from felix.manifests.schema import A2APeerRef, ContentScreening

    answer = {"result": {"artifacts": [{"parts": [{"kind": "text", "text": "here"}, _file_part()]}]}}
    _stub_http(monkeypatch, peers, answer)
    tool = peers.make_peer_tool(A2APeerRef(name="p", url="https://peer.example.com"))

    out = await tool.executor.execute({"message": "send a picture"}, ToolInvocationCtx())
    assert (tool_output_content(out), [a.url for a in tool_output_images(out)]) == ("here", [PNG])

    (screened,) = apply_content_screening([tool], ContentScreening(enabled=True), "m")
    out = await screened.executor.execute({"message": "send a picture"}, ToolInvocationCtx())
    assert tool_output_images(out) == [] and TOOL_IMAGE_UNSCREENED in tool_output_content(out)


# --- MCP client ----------------------------------------------------------------------------


def _image_block(data: str = PNG_B64, media: str = "image/png") -> dict[str, Any]:
    return {"type": "image", "data": data, "mimeType": media}


def test_an_mcp_image_block_becomes_a_checked_tool_image() -> None:
    from felix.mcp.client import _tool_result

    out = _tool_result({"content": [{"type": "text", "text": "chart"}, _image_block(media="text/html")]})
    assert tool_output_content(out) == "chart"
    assert [(a.url, a.media_type) for a in tool_output_images(out)] == [(PNG, "image/png")]


def test_an_image_only_mcp_result_is_not_a_repr_of_the_result() -> None:
    """It fell through to `str(result)`: base64 in the transcript, and no image."""
    from felix.mcp.client import _tool_result

    out = _tool_result({"content": [_image_block()]})
    assert tool_output_content(out) == ""
    assert [a.url for a in tool_output_images(out)] == [PNG]


def test_a_non_image_block_is_dropped_with_a_note() -> None:
    from felix.mcp.client import _tool_result

    out = _tool_result({"content": [_image_block(NOT_AN_IMAGE_B64)]}, tool_name="docs__render")
    assert isinstance(out, str) and "docs__render returned an image that" in out


def test_an_mcp_error_keeps_its_marker_with_or_without_text() -> None:
    from felix.mcp.client import _tool_result

    assert (
        _tool_result({"content": [{"type": "text", "text": "boom"}], "isError": True}) == "[mcp_error] boom"
    )
    image_only = _tool_result({"content": [_image_block()], "isError": True})
    assert tool_output_content(image_only) == "[mcp_error]"
    assert _tool_result({"content": [{"type": "text", "text": "ok"}]}) == "ok"


async def test_the_mcp_client_handler_returns_a_servers_images(monkeypatch: pytest.MonkeyPatch) -> None:
    """The handler's wiring: the helper alone would pass with the handler still text-only."""
    import felix.mcp.client as client_mod
    from felix.manifests.schema import McpServerRef

    class _Resp:
        status_code = 200
        headers: dict[str, str] = {}

        def __init__(self, body: Any) -> None:
            self._body = body

        def raise_for_status(self) -> None:
            return None

        def json(self) -> Any:
            return self._body

    listing = {"tools": [{"name": "render", "description": "Render", "inputSchema": {"type": "object"}}]}

    class _Client:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *a: Any) -> bool:
            return False

        async def post(self, url: str, json: Any = None, headers: Any = None) -> _Resp:
            method = (json or {}).get("method")
            result = {"tools/list": listing, "tools/call": {"content": [_image_block()]}}.get(method, {})
            return _Resp({"jsonrpc": "2.0", "id": 1, "result": result})

    monkeypatch.setattr(client_mod.httpx, "AsyncClient", _Client)
    (tool,) = await client_mod.tools_from_mcp_servers(
        [McpServerRef(name="docs", url="https://mcp.example.com/mcp", transport="http")], allow_http=False
    )
    out = await tool.executor.execute({}, ToolInvocationCtx())
    assert [a.url for a in tool_output_images(out)] == [PNG]


# --- MCP server ----------------------------------------------------------------------------


def _settings() -> Settings:
    return Settings(
        auth_mode="none", allow_insecure=True, object_store="memory", database_url="memory://mcp-images"
    )


async def _call_snap(images: Any, *, tenant: str = "default") -> list[dict[str, Any]]:
    """`tools/call` on Felix's MCP server for a tool returning `images()`, through `handle_rpc`."""
    from felix.context import AuthContext
    from felix.manifests.loader import parse_manifest
    from felix.manifests.store import put_version
    from felix.mcp.server import handle_rpc
    from felix.tools.types import define_tool
    from felix_api.composition import compose

    settings = _settings()

    async def handler(args: dict[str, Any]) -> ToolOutputDict:
        return ToolOutputDict(content="snapped", attachments=await images())

    tools = compose(settings)
    tools.register(
        "snap",
        lambda: define_tool(name="snap", description="d", handler=handler, args_schema={"type": "object"}),
    )
    spec = {"pattern": "react", "tools": ["snap"], "auth": {"inbound": {"allow_anonymous": True}}}
    await put_version(
        settings,
        tenant,
        "snapmcp",
        parse_manifest(
            {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": "snapmcp"}, "spec": spec}
        ),
    )
    resp = await handle_rpc(
        settings=settings,
        tools=tools,
        method="tools/call",
        params={"manifest": "snapmcp", "name": "snap", "arguments": {}},
        rpc_id=1,
        auth=AuthContext(tenant_id=tenant),
    )
    return resp["result"]["content"]


async def test_the_mcp_server_returns_an_image_the_call_stored_as_image_content() -> None:
    """Through `handle_rpc`: the tenant context `_image_blocks` reads under is the call's own."""
    from felix.tools.tool_images import store_image_bytes

    async def stored() -> list[ImageAttachment]:
        url, _ = await store_image_bytes(PNG_BYTES, "image/png", tool_name="snap")
        assert url and url.startswith("felix-file://")
        return [ImageAttachment(url=url), ImageAttachment(url=PNG)]

    content = await _call_snap(stored)
    assert content[0] == {"type": "text", "text": "snapped"}
    assert content[1:] == [{"type": "image", "data": PNG_B64, "mimeType": "image/png"}] * 2


async def test_the_mcp_server_sends_no_url_no_non_image_and_no_reference_it_did_not_make() -> None:
    """A reference a tool merely names may be any upload in the tenant."""
    from felix.attachments import put_attachment
    from felix.storage import get_object_store
    from felix_ai.types import file_ref_url

    settings = _settings()
    uploaded = await put_attachment(
        get_object_store(settings),
        tenant_id="default",
        data=PNG_BYTES,
        media_type="image/png",
        settings=settings,
    )

    async def named() -> list[ImageAttachment]:
        return [
            ImageAttachment(url=file_ref_url(uploaded.file_id)),
            ImageAttachment(url="https://x/y.png"),
            ImageAttachment(url=f"data:image/png;base64,{NOT_AN_IMAGE_B64}"),
        ]

    assert await _call_snap(named) == [{"type": "text", "text": "snapped"}]
