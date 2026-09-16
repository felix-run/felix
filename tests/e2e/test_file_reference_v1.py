"""A stored attachment named in a `/v1` turn, expanded on its way to the model.

The sibling of `test_multimodal_v1.py`: that one sends the bytes inline, this one sends a
reference to bytes already uploaded. The difference is what the session log ends up holding,
which is the reason `POST /files` exists — `full_replay` re-sends that log every turn.

Assertions are on what reached the *model*, because the reply is scripted: a test that
checked only the 200 would pass with the reference dropped anywhere along the way, and
dropping it is exactly what happens if the resolver is never reached.
"""

from __future__ import annotations

import base64
from typing import Any

from felix.manifests.loader import parse_manifest
from felix_ai.providers.scripted import ScriptedTurn

RAW = b"\x89PNG\r\n\x1a\n" + b"pixels" * 16
PNG_B64 = base64.b64encode(RAW).decode("ascii")


def _manifest(name: str) -> Any:
    return parse_manifest(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": name},
            "spec": {
                "pattern": "react",
                "tools": [],
                "auth": {"inbound": {"allow_anonymous": True}},
            },
        }
    )


async def _upload(app: Any) -> str:
    resp = await app.client.post(
        "/files", json={"data": PNG_B64, "media_type": "image/png", "filename": "shot.png"}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["file_id"]


async def test_a_file_reference_reaches_the_model_as_bytes(boot: Any) -> None:
    """`POST /files` → a `file` content part → the compile → react → the model call, with
    the reference expanded to the stored bytes by the time a provider sees it."""
    async with boot([ScriptedTurn(content="a logo")], manifests={"e2e-files": _manifest("e2e-files")}) as app:
        file_id = await _upload(app)

        resp = await app.client.post(
            "/v1/chat/completions",
            json={
                "model": "e2e-files",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "what is in this picture?"},
                            {"type": "file", "file": {"file_id": file_id}},
                        ],
                    }
                ],
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["choices"][0]["message"]["content"] == "a logo"

        seen = [m for call in app.spy.prompts for m in call]
        urls = [b.url for m in seen for b in (getattr(m, "content_blocks", None) or []) if b.url]
        assert urls, "the reference never reached the model at all"
        assert all(u.startswith("data:image/png;base64,") for u in urls), urls
        assert base64.b64decode(urls[0].split(",", 1)[1]) == RAW
        # And nothing that still needs resolving got as far as a provider.
        assert not any("felix-file://" in u for u in urls)


async def test_a_reference_to_nothing_does_not_fail_the_turn(boot: Any) -> None:
    """An id that names nothing is dropped and the turn still answers.

    Refusing instead would mean a thread stops answering the moment an attachment is
    deleted, because the turn naming it is already in an append-only log.
    """
    async with boot(
        [ScriptedTurn(content="I see no picture")], manifests={"e2e-files": _manifest("e2e-files")}
    ) as app:
        resp = await app.client.post(
            "/v1/chat/completions",
            json={
                "model": "e2e-files",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "what is in this picture?"},
                            {"type": "file", "file": {"file_id": "0" * 32}},
                        ],
                    }
                ],
            },
        )
        assert resp.status_code == 200, resp.text

        seen = [m for call in app.spy.prompts for m in call]
        blocks = [b for m in seen for b in (getattr(m, "content_blocks", None) or [])]
        # Text first: if the resolver raised and react degraded to an error reply there
        # would be no blocks at all, and the url assertion would pass for that reason.
        assert any("what is in this picture?" in (b.text or "") for b in blocks)
        assert not [b for b in blocks if b.url], "a dangling reference reached the model"


async def test_the_log_keeps_the_reference_while_the_model_gets_the_bytes(boot: Any) -> None:
    """The pair, asserted as a pair. Either half alone is the bug this feature exists to avoid.

    Turn one names the file; turn two says something else on the same thread. What the model
    is handed on turn two must be the *bytes*, replayed out of the session log — and what the
    log holds must still be the *reference*. Expanding at ingest satisfies the first half and
    quietly fails the second: the thread keeps working, it just re-sends 600 KiB every turn,
    which is the whole cost `POST /files` exists to avoid.

    So the export assertion is the load-bearing one. It is what goes red if resolution ever
    migrates earlier — into the route, or into `ChatMessage.model_validate` — and nothing else
    here would notice.

    Reading `prompts[-1]` rather than `[0]` is what makes the first half about replay: with a
    single turn the assertion is satisfied by the message being sent right now, and a thread id
    that never worked would look identical.
    """
    async with boot(
        [ScriptedTurn(content="a logo"), ScriptedTurn(content="still a logo")],
        manifests={"e2e-files": _manifest("e2e-files")},
    ) as app:
        file_id = await _upload(app)
        thread = "files-thread"

        for question, parts in (
            ("what is in this picture?", [{"type": "file", "file": {"file_id": file_id}}]),
            ("are you sure?", []),
        ):
            resp = await app.client.post(
                "/v1/chat/completions",
                json={
                    "model": "e2e-files",
                    "user": thread,
                    "messages": [
                        {
                            "role": "user",
                            "content": [{"type": "text", "text": question}, *parts],
                        }
                    ],
                },
            )
            assert resp.status_code == 200, resp.text

        replayed = app.spy.prompts[-1]
        carried = [a.url for m in replayed for a in (getattr(m, "attachments", None) or [])]
        carried += [b.url for m in replayed for b in (getattr(m, "content_blocks", None) or []) if b.url]
        assert any(u.startswith("data:image/png;base64,") for u in carried), carried
        assert base64.b64decode(next(u for u in carried).split(",", 1)[1]) == RAW
        assert not any("felix-file://" in u for u in carried)
        assert any("what is in this picture?" in (getattr(m, "content", "") or "") for m in replayed), (
            "turn one was never replayed, so the assertion above is about a thread that was never continued"
        )

        export = await app.client.get(f"/chat/sessions/{thread}/export")
        assert export.status_code == 200, export.text
        body = export.text
        assert f"felix-file://{file_id}" in body, "the log stopped holding the reference"
        assert "base64" not in body, "the log holds the expansion — resolution moved too early"


async def test_the_streamed_path_expands_a_reference_too(boot: Any) -> None:
    """`stream_turn` is a separate call site from `chat`, and it is the one a real client
    takes: `/chat` SSE and `stream: true` both land there.

    Worth its own test because the failure is silent rather than loud. `inline_parts` drops an
    unexpanded reference, so a regression here does not 400 — the model is handed a turn with
    no image and answers plausibly about nothing.
    """
    async with boot([ScriptedTurn(content="a logo")], manifests={"e2e-files": _manifest("e2e-files")}) as app:
        file_id = await _upload(app)

        async with app.client.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "model": "e2e-files",
                "stream": True,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "what is in this picture?"},
                            {"type": "file", "file": {"file_id": file_id}},
                        ],
                    }
                ],
            },
        ) as resp:
            assert resp.status_code == 200, resp.text
            async for _ in resp.aiter_lines():
                pass

        seen = [m for call in app.spy.prompts for m in call]
        urls = [b.url for m in seen for b in (getattr(m, "content_blocks", None) or []) if b.url]
        urls += [a.url for m in seen for a in (getattr(m, "attachments", None) or [])]
        # Named, not assumed: if `stream: true` ever routed to `chat` this test would be
        # a second copy of the one above and the streamed call site would be uncovered.
        assert app.spy.calls == ["stream_turn"], app.spy.calls
        assert urls, "the streamed call never saw the reference at all"
        assert all(u.startswith("data:image/png;base64,") for u in urls), urls
