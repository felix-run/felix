"""A stored attachment, named in a turn and expanded only at the wire.

The trade this makes is the reason `POST /files` exists at all. An image sent inline lands
in the session event log, and `full_replay` re-sends that log on every later turn — so a
600 KiB screenshot attached on turn one is re-uploaded to the model on turns two, three and
four. A reference is small enough to replay; the bytes are fetched per turn instead.

So the two properties worth pinning are a pair, and either alone is the bug: what reaches
the model is bytes, and what the session keeps is the reference.
"""

from __future__ import annotations

import base64

import pytest
from felix.attachments import put_attachment, resolve_file_refs, sniff_media_type
from felix.config import Settings
from felix.storage import get_object_store
from felix_ai.types import ChatMessage, file_ref_url

PNG = b"\x89PNG\r\n\x1a\n" + b"payload" * 8
GIF = b"GIF89a" + b"payload" * 8


def _settings() -> Settings:
    return Settings(object_store="memory", database_url="memory://refs", allow_insecure=True)


async def _stored(settings, tenant_id: str, raw: bytes, media_type: str) -> str:
    att = await put_attachment(
        get_object_store(settings),
        tenant_id=tenant_id,
        data=raw,
        media_type=media_type,
        settings=settings,
    )
    return att.file_id


def _turn(*refs: str, text: str = "what is this") -> ChatMessage:
    return ChatMessage.model_validate(
        {
            "role": "user",
            "content": [{"type": "text", "text": text}]
            + [{"type": "file", "file": {"file_id": r}} for r in refs],
        }
    )


async def _resolve(settings, messages, tenant_id: str = "acme"):
    return await resolve_file_refs(messages, tenant_id=tenant_id, object_store=get_object_store(settings))


@pytest.mark.asyncio
async def test_a_reference_reaches_the_model_as_the_bytes_it_names() -> None:
    settings = _settings()
    file_id = await _stored(settings, "acme", PNG, "image/png")

    [resolved] = await _resolve(settings, [_turn(file_id)])

    url = resolved.attachments[0].url
    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == PNG
    # Both shapes, because a request renders from one and a replayed turn from the other.
    assert resolved.content_blocks[1].url == url


@pytest.mark.asyncio
async def test_the_media_type_is_read_off_the_bytes_not_a_stored_label() -> None:
    """There is no stored label on the default backend — `storage/fs.py` discards the
    content type — so a sniff is the only answer that is right on every backend."""
    settings = _settings()
    file_id = await _stored(settings, "acme", GIF, "image/gif")

    [resolved] = await _resolve(settings, [_turn(file_id)])

    assert resolved.attachments[0].url.startswith("data:image/gif;base64,")
    # The field too, not just the url. A reference sent without an explicit type is
    # defaulted to `image/png` at parse time, so leaving it would put `image/png` next to
    # a `data:image/gif` url. The wires re-derive from the url and would not notice.
    assert resolved.attachments[0].media_type == "image/gif"
    assert resolved.content_blocks[1].media_type == "image/gif"

    assert sniff_media_type(PNG) == "image/png"
    assert sniff_media_type(b"not an image") is None
    # `RIFF` alone is WAV and AVI as well, and sniffing is now the only thing deciding what
    # a model is told these bytes are — so the container's format tag has to be checked.
    assert sniff_media_type(b"RIFF" + b"\x00" * 4 + b"WEBPVP8 ") == "image/webp"
    assert sniff_media_type(b"RIFF" + b"\x00" * 4 + b"WAVEfmt ") is None


@pytest.mark.asyncio
async def test_one_tenant_cannot_name_another_tenants_file() -> None:
    """The tenant comes from the request context; the reference only carries an id.

    A hit for the wrong tenant would be a cross-tenant read with no route involved, since
    this runs below every door.
    """
    settings = _settings()
    file_id = await _stored(settings, "globex", PNG, "image/png")

    [resolved] = await _resolve(settings, [_turn(file_id)], tenant_id="acme")

    assert not resolved.attachments, "acme resolved a file globex owns"
    assert [b.type for b in resolved.content_blocks] == ["text"]


@pytest.mark.asyncio
async def test_a_deleted_attachment_drops_rather_than_breaking_the_thread() -> None:
    """Raising here would make a thread unanswerable forever the moment an attachment is
    deleted: the turn is already in an append-only log, so every later turn replays it."""
    settings = _settings()

    [resolved] = await _resolve(settings, [_turn("0" * 32)])

    assert not resolved.attachments
    assert resolved.content_blocks[0].text == "what is this"


@pytest.mark.asyncio
async def test_a_turn_that_was_only_a_reference_still_says_something() -> None:
    """Dropping a reference must not leave an empty message.

    A turn whose only content part was the file has no text to fall back on, so dropping
    the part left `content=""` — and an empty content is an Anthropic 400 in its own
    right. That is the same wedged-thread outage dropping-rather-than-raising exists to
    prevent, reached by the other road: the turn is in an append-only log, so every later
    turn replays it and fails at the provider forever.

    The marker also stops the model answering confidently about an image it never saw.
    """
    settings = _settings()

    [resolved] = await _resolve(settings, [_turn("0" * 32, text="")])

    assert resolved.content, "the message would reach the provider empty"
    assert resolved.content_blocks and resolved.content_blocks[0].text
    assert "0" * 32 in resolved.content_blocks[0].text


@pytest.mark.asyncio
async def test_an_unparseable_reference_is_not_echoed_back_at_the_model() -> None:
    """The marker reaches the model, and a reference is caller-written — so the id is
    echoed only when `valid_file_id` accepts it. The one that failed to resolve is the one
    most likely to have been written to be read."""
    settings = _settings()

    [resolved] = await _resolve(settings, [_turn("../../etc/passwd", text="")])

    text = resolved.content_blocks[0].text or ""
    assert "etc/passwd" not in text
    assert "no longer available" in text


@pytest.mark.asyncio
async def test_one_reference_repeated_across_a_replayed_context_is_read_once() -> None:
    """`full_replay` re-sends every prior turn, so one image arrives once per turn in a
    single render — five messages, one object.

    The memo is per *call*, deliberately: across turns the bytes are re-read, which is
    what keeps a deleted attachment from being served from a stale cache. So this pins
    five messages in one render, not five turns; do not read it as covering a
    process-wide cache, which would be a different decision with a different risk."""
    settings = _settings()
    file_id = await _stored(settings, "acme", PNG, "image/png")
    store = get_object_store(settings)
    reads: list[str] = []
    original = store.get

    async def counting_get(key: str):
        reads.append(key)
        return await original(key)

    store.get = counting_get  # type: ignore[method-assign]
    try:
        resolved = await resolve_file_refs(
            [_turn(file_id) for _ in range(5)], tenant_id="acme", object_store=store
        )
    finally:
        store.get = original  # type: ignore[method-assign]

    assert len(reads) == 1, f"read the same object {len(reads)} times"
    assert all(m.attachments[0].url.startswith("data:image/png") for m in resolved)


@pytest.mark.asyncio
async def test_messages_without_a_reference_come_back_untouched() -> None:
    """The overwhelmingly common message. Identity rather than equality, because a copy
    per turn on every model call would be the cost of a feature nobody used."""
    settings = _settings()
    plain = ChatMessage.model_validate({"role": "user", "content": "no attachments here"})

    resolved = await _resolve(settings, [plain])

    assert resolved[0] is plain


@pytest.mark.asyncio
async def test_resolving_does_not_rewrite_the_caller_s_message() -> None:
    """These belong to the session, and the next turn is rebuilt from them. Expanding one
    in place would put the base64 back in the log, which is the thing being avoided."""
    settings = _settings()
    file_id = await _stored(settings, "acme", PNG, "image/png")
    original = _turn(file_id)

    await _resolve(settings, [original])

    assert original.attachments[0].url == file_ref_url(file_id)
    assert original.content_blocks[1].url == file_ref_url(file_id)


@pytest.mark.asyncio
async def test_the_session_serialisers_carry_a_reference_through_unchanged() -> None:
    """`session/types.py` persists `attachments[].url` and restores it, so a reference
    survives a turn as a reference rather than as bytes.

    Scope, stated because it is narrower than it looks: this exercises the two
    serialisers on a message built here, and the resolver never runs in it. It cannot
    tell you *when* expansion happens, so it does not defend the decision the feature
    turns on — move resolution to ingest and this stays green. The test that goes red is
    `tests/e2e/test_file_reference_v1.py::test_the_log_keeps_the_reference_while_the_model_gets_the_bytes`, which asserts on the exported log.
    """
    from felix.session.types import chat_message_to_event, event_to_chat_message

    file_id = "b" * 32
    event = chat_message_to_event(_turn(file_id))

    stored = event.metadata["attachments"][0]["url"]
    assert stored == file_ref_url(file_id)
    assert "base64" not in stored

    class _Restored:
        kind, role, content = "message", "user", "what is this"
        tool_call_id = name = None
        tool_calls: list = []
        metadata = event.metadata

    assert event_to_chat_message(_Restored()).attachments[0].url == file_ref_url(file_id)
