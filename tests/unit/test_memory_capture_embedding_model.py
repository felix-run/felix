"""Captured vectors record the model that made them, not the setting.

A hosted embedder with `FELIX_MEMORY_EMBEDDING_MODEL` empty uses its provider's default
(`@cf/baai/bge-base-en-v1.5` on Workers AI), so recording the setting stamped `""` on every
vector it wrote — and a later model change could not tell which rows to re-embed.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from felix.manifests.schema import MemoryCapture
from felix.memory import embedder as embedder_mod
from felix.memory import store as memory_store
from felix.memory.capture import capture_from_turn
from felix.memory.store import MemoryPool

from tests.support.factories import make_settings

TENANT = "t-embed-model"


class _HostedEmbedder:
    enabled = True
    dim = 3
    model = "@cf/baai/bge-base-en-v1.5"

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[0.1, 0.2, 0.3] for _ in texts]


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    memory_store._memory_rows.clear()
    monkeypatch.setattr(embedder_mod, "_backends", dict(embedder_mod._backends))
    embedder_mod.register_embedder_backend("hosted-fake", lambda _s: _HostedEmbedder())


async def test_a_captured_vector_records_the_backends_model() -> None:
    settings = make_settings(memory_embedder="hosted-fake", memory_embedding_model="")
    await capture_from_turn(
        settings,
        TENANT,
        pool=MemoryPool.agent("m"),
        user_text="Where is the runbook?",
        assistant_text="The deploy runbook lives in the ops repository under runbooks/deploy.md.",
        capture=MemoryCapture(enabled=True, max_facts=3, min_chars=5),
        model=None,
    )
    rows = list(memory_store._memory_rows.values())
    assert rows, "the heuristic path stored nothing to check"
    assert {r["embedding_model"] for r in rows} == {"@cf/baai/bge-base-en-v1.5"}


class _Anonymous:
    """A registered backend that names no model -- a plugin, typically."""

    enabled = True
    dim = 3

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[0.1, 0.2, 0.3] for _ in texts]


def test_a_backend_naming_no_model_records_the_setting() -> None:
    """`build_embedder` is the one place the setting stands in, so every caller reads
    `embedder.model` and none guesses; a built-in's own model is never overwritten."""
    embedder_mod.register_embedder_backend("anonymous-fake", lambda _s: _Anonymous())

    built = embedder_mod.build_embedder(
        make_settings(memory_embedder="anonymous-fake", memory_embedding_model="custom-768")
    )
    assert built.model == "custom-768"
    hosted = embedder_mod.build_embedder(
        make_settings(memory_embedder="hosted-fake", memory_embedding_model="custom-768")
    )
    assert hosted.model == "@cf/baai/bge-base-en-v1.5"
    assert embedder_mod.build_embedder(make_settings(memory_embedder="none")).model == ""
