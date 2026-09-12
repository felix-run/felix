"""An image sent inline, which is the way an OpenAI SDK sends one.

A caller attaches an image as a `data:` URL — that is what OpenAI's own API documents, so it
is what every SDK emits and what a caller copies out of those docs. Felix accepted it on
`/chat`, both wires had image encoders, and it still could not work end to end:

* the Anthropic wire put the data URL in a `url` source, which that API rejects — so an image
  reached `gpt-4o` and 400'd on `claude-sonnet`, this harness's default model;
* `/v1/chat/completions` typed `content` as `str | None`, so a multimodal request was a 422
  before any of the above ran.

The cross-wire statement — the same message reaches either provider in the form that provider
accepts — is in `tests/conformance/test_model_provider.py`. What is here is the per-wire detail
a contract cannot state, and the parsing that decides what an inline image even is.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest
from felix_ai.types import ChatMessage, ModelRoute
from felix_ai.wire.anthropic_messages import AnthropicMessagesClient
from felix_ai.wire.base import split_data_url
from felix_ai.wire.openai_completions import OpenAICompletionsClient

PNG = "data:image/png;base64,iVBORw0KGgo="
JPEG = "data:image/jpeg;base64,/9j/4AAQSkZJRg=="
REMOTE = "https://example.com/cat.png"


def _message(url: str, text: str = "what is this?") -> ChatMessage:
    """The message an OpenAI SDK sends for an image, through the real parser."""
    return ChatMessage.model_validate(
        {
            "role": "user",
            "content": [
                {"type": "text", "text": text},
                {"type": "image_url", "image_url": {"url": url}},
            ],
        }
    )


def _settings() -> Any:
    return type("_S", (), {"model_timeout_seconds": 30})()


def _anthropic_content(msg: ChatMessage) -> list[dict[str, Any]]:
    client = AnthropicMessagesClient(
        model_id="claude-sonnet-5",
        route=ModelRoute(provider="anthropic", model="claude-sonnet-5"),
        settings=_settings(),
        spec=None,
        base_url="https://example.invalid",
        api_key="k",
    )
    return client._body([msg], [], 0.0, 256)["messages"][0]["content"]


def _openai_content(msg: ChatMessage) -> list[dict[str, Any]]:
    client = OpenAICompletionsClient(
        model_id="gpt-4o",
        route=ModelRoute(provider="openai", model="gpt-4o"),
        settings=_settings(),
        spec=None,
        base_url="https://example.invalid/v1",
        api_key="k",
    )
    return client._body([msg], [], 0.0, 256)["messages"][0]["content"]


# --- what counts as an inline image ---------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (PNG, ("image/png", "iVBORw0KGgo=")),
        (JPEG, ("image/jpeg", "/9j/4AAQSkZJRg==")),
        # Parameters may sit between the type and the marker, so the marker is looked for at
        # the end and the type at the front.
        ("data:image/png;charset=binary;base64,AAA=", ("image/png", "AAA=")),
        # No media type at all is legal and means the default.
        ("data:;base64,AAA=", ("application/octet-stream", "AAA=")),
    ],
)
def test_a_base64_data_url_splits_into_media_type_and_payload(url: str, expected: tuple[str, str]) -> None:
    assert split_data_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        REMOTE,
        "",
        # Percent-encoded rather than base64. `split_data_url` reports what a URL *is*;
        # `canonical_inline_url` is what converts one, and they are separate so that the
        # conversion happens once, above both wires, rather than inside each reader.
        "data:text/plain,hello",
        # No comma: not a data URL at all.
        "data:image/png;base64",
    ],
)
def test_anything_else_is_not_an_inline_image(url: str) -> None:
    assert split_data_url(url) is None


# --- Anthropic, which has no URL form for inline bytes --------------------------------------


def test_an_inline_image_reaches_anthropic_as_base64() -> None:
    """The defect this branch exists for. A `data:` URL in a `url` source is a 400 from that
    API, so every inline image failed on the harness's default model while working on
    OpenAI — the shape of bug that looks like a vendor outage rather than a Felix one."""
    blocks = _anthropic_content(_message(PNG))
    assert blocks[0] == {"type": "text", "text": "what is this?"}
    assert blocks[1] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="},
    }


def test_the_media_type_comes_from_the_data_url_not_the_parse_default() -> None:
    """`ContentBlock.media_type` is `image/png` for anything that did not say otherwise —
    a parse default, not the caller's declaration. Sending it for a JPEG is a provider error,
    and it is invisible in a test that only ever uses PNGs."""
    blocks = _anthropic_content(_message(JPEG))
    assert blocks[1]["source"]["media_type"] == "image/jpeg"
    assert blocks[1]["source"]["data"] == "/9j/4AAQSkZJRg=="


def test_a_remote_url_still_goes_as_a_url() -> None:
    """The counterpart: the fix must not turn every image into an inline one. Anthropic
    fetches a `url` source itself, which is the whole point of having the form."""
    blocks = _anthropic_content(_message(REMOTE))
    assert blocks[1]["source"] == {
        "type": "url",
        "url": REMOTE,
        "media_type": "image/png",
    }


def test_a_percent_encoded_data_url_is_re_encoded_rather_than_dropped() -> None:
    """`data:image/svg+xml,<svg …>` is a legal, ordinary way to write an image inline, and
    neither provider accepts it. The first version of this branch dropped it with a warning,
    which loses a valid image — and, for a message carrying nothing else, left an empty
    content that Anthropic rejects outright, turning one bad block into a lost turn.

    Re-*labelling* those bytes as base64 would be a lie. Re-*encoding* them is a conversion,
    and it is what makes the two wires agree: before this, a malformed inline image was a hard
    400 on one route and a confidently wrong answer on the other, decided by nothing more than
    which model the manifest happened to route to.
    """
    svg = "data:image/svg+xml,%3Csvg%3E%3C/svg%3E"
    blocks = _anthropic_content(_message(svg))
    assert blocks[1]["source"]["type"] == "base64"
    assert blocks[1]["source"]["media_type"] == "image/svg+xml"
    assert base64.b64decode(blocks[1]["source"]["data"]).decode() == "<svg></svg>"


def test_an_image_only_message_survives_the_conversion() -> None:
    """The case the drop could not serve: nothing else in the message to fall back to. An
    empty `content` is itself an Anthropic 400, so the turn died with an error about content
    rather than about the image — strictly harder to diagnose than the bug being fixed."""
    msg = ChatMessage.model_validate(
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png,AA"}}]}
    )
    blocks = _anthropic_content(msg)
    assert isinstance(blocks, list) and blocks, "an image-only turn must not become empty content"
    assert blocks[0]["source"]["type"] == "base64"


def test_the_older_attachments_shape_takes_the_same_path() -> None:
    """`attachments` predates `content_blocks` and carries the same URLs, so it had the same
    bug. One encoder for both is what stops the two drifting again."""
    msg = ChatMessage.model_validate(
        {"role": "user", "content": "look", "attachments": [{"url": JPEG, "media_type": "image/jpeg"}]}
    )
    blocks = _anthropic_content(msg)
    assert blocks[1]["source"]["type"] == "base64"
    assert blocks[1]["source"]["media_type"] == "image/jpeg"


# --- OpenAI, where a data URL is native ------------------------------------------------------


def test_openai_sends_the_data_url_verbatim() -> None:
    """Nothing to convert, and this pins that the shared splitter was not applied here too —
    OpenAI takes the whole URL, and taking it apart would break the one wire that worked."""
    blocks = _openai_content(_message(PNG))
    assert blocks[1] == {"type": "image_url", "image_url": {"url": PNG}}


def test_both_wires_carry_the_same_bytes() -> None:
    """The property that matters to a caller: whichever provider a manifest routes to, the
    image it sent is the image the model sees."""
    anthropic = _anthropic_content(_message(JPEG))[1]["source"]
    openai = _openai_content(_message(JPEG))[1]["image_url"]["url"]
    assert openai == f"data:{anthropic['media_type']};base64,{anthropic['data']}"
    assert json.loads(json.dumps(anthropic)) == anthropic, "the block must survive the body"


# --- shape, which one encoder for two message shapes is what preserves --------------------------


def test_caller_order_is_preserved_across_both_wires() -> None:
    """An image-then-text message means something different from text-then-image, and the two
    message shapes used to disagree: the blocks branch kept the caller's order while the
    attachments fallback always emitted text first. A thread hits both — turn one parses into
    blocks, every later turn is replayed as attachments — so the order of a question and its
    picture could change between turn one and turn two of one conversation."""
    msg = ChatMessage.model_validate(
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": PNG}},
                {"type": "text", "text": "and what about that one?"},
            ],
        }
    )
    assert [b["type"] for b in _anthropic_content(msg)] == ["image", "text"]
    assert [b["type"] for b in _openai_content(msg)] == ["image_url", "text"]


def test_every_image_in_a_message_is_sent() -> None:
    """Two pictures and one question is an ordinary comparison prompt, and nothing pinned that
    a wire sent both rather than the first or a deduplicated one."""
    msg = ChatMessage.model_validate(
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "which is bigger?"},
                {"type": "image_url", "image_url": {"url": PNG}},
                {"type": "image_url", "image_url": {"url": JPEG}},
            ],
        }
    )
    sources = [b["source"]["data"] for b in _anthropic_content(msg) if b["type"] == "image"]
    assert sources == ["iVBORw0KGgo=", "/9j/4AAQSkZJRg=="]
    urls = [b["image_url"]["url"] for b in _openai_content(msg) if b["type"] == "image_url"]
    assert urls == [PNG, JPEG]


def test_every_image_survives_the_replay_shape_too() -> None:
    """The same claim against `attachments`, which is the shape a second turn arrives in —
    `session/types.py` restores that one and not `content_blocks`. A test that only ever built
    messages the way a *request* parses would pin one of the two branches a thread uses."""
    msg = ChatMessage.model_validate(
        {
            "role": "user",
            "content": "which is bigger?",
            "attachments": [{"url": PNG}, {"url": JPEG, "media_type": "image/jpeg"}],
        }
    )
    sources = [b["source"] for b in _anthropic_content(msg) if b["type"] == "image"]
    assert [src["data"] for src in sources] == ["iVBORw0KGgo=", "/9j/4AAQSkZJRg=="]
    assert [src["media_type"] for src in sources] == ["image/png", "image/jpeg"]


def test_an_unrecognised_content_part_is_reported_rather_than_vanishing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`/v1` accepts an open list of parts so that what Felix does with a part it does not know
    is decided in one place. Falling through the loop is not a decision — before this, an
    `input_audio` part turned a 422 into a 200 with the caller's payload gone and nothing in
    the log to say so."""
    with caplog.at_level("WARNING", logger="felix_ai.types"):
        msg = ChatMessage.model_validate(
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "listen"},
                    {"type": "input_audio", "input_audio": {"data": "AAA="}},
                ],
            }
        )
    assert msg.content == "listen", "the parts that are understood still arrive"
    assert "input_audio" in caplog.text
