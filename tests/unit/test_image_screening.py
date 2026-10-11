"""`content_screening.image_model` at the unit level: the schema rule, and the decider-down path.

Images are screened only when `content_screening.enabled` is true, so a manifest naming an
image model with screening off would read as governed and screen nothing. The behaviour
itself is covered through the stack in `tests/e2e/test_image_screening.py`.
"""

from __future__ import annotations

import pytest
from felix.manifests.schema import ContentScreening


def test_an_image_model_with_screening_off_is_refused() -> None:
    with pytest.raises(ValueError, match=r"image_model needs content_screening\.enabled"):
        ContentScreening(image_model="claude-sonnet")


def test_an_image_model_with_screening_on_is_accepted() -> None:
    assert ContentScreening(enabled=True, image_model="claude-sonnet").image_model == "claude-sonnet"


def test_images_are_unscreened_by_default() -> None:
    assert ContentScreening(enabled=True).image_model == ""


async def test_an_unbuildable_decider_quarantines_images_without_transcribing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The operator asked for the decider on everything screened; it cannot be built, so no
    image has been cleared — and none is worth paying to transcribe.

    A unit test because the compile binds the same decider and fails the request first, so
    over HTTP this path is reached only when screening and the compile disagree.
    """
    from felix.config import Settings
    from felix.governance import image_screening, inbound
    from felix.governance.image_screening import QUARANTINED_UNSCREENED
    from felix.manifests.loader import parse_manifest
    from felix_ai.types import ChatMessage, ContentBlock

    def cannot_build(*_: object) -> None:
        raise ValueError("no route")

    async def never(*_: object) -> str:
        raise AssertionError("an image was transcribed while its decider was down")

    monkeypatch.setattr(inbound, "screening_decider", cannot_build)
    monkeypatch.setattr(image_screening, "_transcribe", never)
    manifest = parse_manifest(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": "img"},
            "spec": {
                "decider": {"id": "jev"},
                "content_screening": {"enabled": True, "decider": True, "image_model": "claude-sonnet"},
            },
        }
    )
    png = "data:image/png;base64,iVBORw0KGgo="
    turn = ChatMessage(
        role="user",
        content="look",
        content_blocks=[ContentBlock(type="text", text="look"), ContentBlock(type="image_url", url=png)],
    )

    (out,) = await inbound.apply_inbound_screening(manifest, [turn], Settings())

    assert not [b for b in out.content_blocks or [] if b.url]
    assert QUARANTINED_UNSCREENED in " ".join(b.text or "" for b in out.content_blocks or [])
