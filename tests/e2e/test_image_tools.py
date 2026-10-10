"""`spec.image_tools` through the real app: bound from the manifest, fed the user's image, seen.

The user's image arrives on `/chat`; the model asks for a thumbnail of `latest`; the next
model call must be handed the thumbnail as an image -- which needs the schema field, the
builder's binder, the session log lookup, the store and the runner all wired, none of which a
unit test of the tool alone can show.
"""

from __future__ import annotations

import base64
import io
from typing import Any

from felix.manifests.loader import parse_manifest
from felix_ai.providers.scripted import ScriptedTurn
from felix_ai.types import ToolCall

from tests.support.optional_deps import require_optional


def _png(size: tuple[int, int]) -> str:
    image = require_optional("PIL.Image", "image")
    buf = io.BytesIO()
    image.new("RGB", size, "blue").save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _manifest() -> Any:
    spec = {
        "pattern": "react",
        "tools": [],
        "image_tools": [{"name": "shrink", "op": "thumbnail"}, {"name": "images", "op": "list"}],
        "auth": {"inbound": {"allow_anonymous": True}},
    }
    return parse_manifest(
        {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": "editor"}, "spec": spec}
    )


async def test_the_model_sees_the_thumbnail_it_asked_for_and_can_chain_on_it(boot: Any) -> None:
    """Three tool calls through the runner: shrink the upload, list the thread, shrink `latest`.

    The second shrink must take the *first one's result*, which only the session log written
    between rounds can hand it -- the chain no unit test of the tool alone reaches.
    """
    image = require_optional("PIL.Image", "image")
    script = [
        ScriptedTurn(tool_calls=[ToolCall(id="c1", name="shrink", args={"max_size": 32})]),
        ScriptedTurn(tool_calls=[ToolCall(id="c2", name="images", args={})]),
        ScriptedTurn(tool_calls=[ToolCall(id="c3", name="shrink", args={"max_size": 16})]),
        ScriptedTurn(content="a small blue square"),
    ]
    turn = {
        "role": "user",
        "content": [
            {"type": "text", "text": "make this smaller"},
            {"type": "image_url", "image_url": {"url": _png((256, 128))}},
        ],
    }
    async with boot(script, manifests={"editor": _manifest()}) as app:
        resp = await app.client.post(
            "/chat", json={"manifest": "editor", "thread_id": "edit", "messages": [turn]}
        )
        assert resp.status_code == 200, resp.text
        assert {"shrink", "images"} <= set(app.spy.tools[0]), "both ops bound from the manifest"

        def tool_messages(call: int) -> list[Any]:
            return [m for m in app.spy.prompts[call] if m.role == "tool"]

        first = tool_messages(1)[-1]
        assert "Stored as felix-file://" in first.content, first.content
        ref = first.content.split("Stored as ", 1)[1].rstrip(".")
        assert _size(image, first) == (32, 16), "the thumbnail, resolved to bytes at the wire"

        listed = tool_messages(2)[-1].content.splitlines()
        assert listed == ["#1: (inline) from the user", f"#2: {ref} returned by shrink"]

        assert _size(image, tool_messages(3)[-1]) == (16, 8), "`latest` was the first result"


def _size(image: Any, tool_msg: Any) -> tuple[int, int]:
    (att,) = tool_msg.attachments or []
    return image.open(io.BytesIO(base64.b64decode(att.url.split(",", 1)[1]))).size
