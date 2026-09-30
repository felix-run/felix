"""`FELIX_MEMORY_EMBEDDER=auto`, the default: local when the extra is installed, off otherwise.

`scripts/test.sh` pins `none` so the suite never loads a model, which leaves the default itself
reached by nothing but this file.
"""

from __future__ import annotations

import importlib.util
from types import SimpleNamespace

import pytest
from felix.config import Settings
from felix.memory.embedder import NullEmbedder, SentenceTransformersEmbedder, build_embedder


def test_the_default_is_auto() -> None:
    assert Settings.model_fields["memory_embedder"].default == "auto"


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        memory_embedder="auto", memory_embedding_model="bge-base-en-v1.5", memory_embedding_dim=768
    )


def test_auto_embeds_locally_when_the_extra_is_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    real = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name, *a: object() if name == "sentence_transformers" else real(name, *a),
    )
    embedder = build_embedder(_settings())  # type: ignore[arg-type]
    assert isinstance(embedder, SentenceTransformersEmbedder)


def test_auto_turns_the_vector_channel_off_without_it(monkeypatch: pytest.MonkeyPatch) -> None:
    real = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name, *a: None if name == "sentence_transformers" else real(name, *a),
    )
    assert isinstance(build_embedder(_settings()), NullEmbedder)  # type: ignore[arg-type]
