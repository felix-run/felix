"""The session index's preview: the cut, the mask, and the manifest a turn records.

The store rules (written once, read as null when absent) are in
`tests/conformance/test_thread_state.py`, on both arms. These are the parts with no store in
them: how a message becomes a preview, and what `ensure_thread_pin` writes for the index.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.manifests.loader import parse_manifest
from felix.manifests.pin import ensure_thread_pin
from felix.session.thread_state import PREVIEW_CHARS, get_thread_meta, note_first_message, thread_preview


def _agent(name: str, **spec: Any) -> Any:
    return parse_manifest(
        {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": name}, "spec": spec}
    )


def test_whitespace_collapses_before_the_cut() -> None:
    assert thread_preview("a\n\n\tb   c") == "a b c"
    assert thread_preview("   ") is None
    assert thread_preview("") is None
    assert thread_preview(None) is None


def test_a_long_message_is_cut_with_an_ellipsis_and_a_short_one_is_not() -> None:
    exact = "x" * PREVIEW_CHARS
    assert thread_preview(exact) == exact
    cut = thread_preview("word " * 100)
    assert cut is not None
    assert len(cut) <= PREVIEW_CHARS
    assert cut.endswith("…")
    assert not cut[:-1].endswith(" "), "the cut leaves no trailing space before the ellipsis"


@pytest.mark.asyncio
async def test_a_secret_in_the_first_message_is_masked_in_the_preview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import felix.secrets as secrets_mod

    secret = "super-secret-value-9f2b"
    monkeypatch.setattr(secrets_mod, "collected_secret_values", lambda *a, **k: [secret])
    thread = "t:masked"
    await note_first_message(settings=None, tenant_id="t", thread_id=thread, text=f"the key is {secret}")
    meta = await get_thread_meta(settings=None, tenant_id="t", thread_id=thread)
    assert meta["preview"] == "the key is [REDACTED]"


@pytest.mark.asyncio
async def test_the_pin_records_the_manifest_a_thread_moves_to() -> None:
    """Without `pin_compile` a thread may change manifests; the index follows the newest."""
    thread = "t:moves"
    await ensure_thread_pin(settings=None, tenant_id="t", thread_id=thread, manifest=_agent("quick"))
    meta = await get_thread_meta(settings=None, tenant_id="t", thread_id=thread)
    assert (meta["manifest_name"], meta["last_manifest"]) == ("quick", "quick")
    revision = meta["revision"]

    await ensure_thread_pin(settings=None, tenant_id="t", thread_id=thread, manifest=_agent("quick"))
    meta = await get_thread_meta(settings=None, tenant_id="t", thread_id=thread)
    assert meta["revision"] == revision, "an unchanged manifest wrote anyway"

    await ensure_thread_pin(settings=None, tenant_id="t", thread_id=thread, manifest=_agent("cowork"))
    meta = await get_thread_meta(settings=None, tenant_id="t", thread_id=thread)
    assert meta["manifest_name"] == "quick", "the pin's first-touch record is not the index's to move"
    assert meta["last_manifest"] == "cowork"
