"""Text embedding behind a protocol, so semantic recall is optional.

The default is :class:`NullEmbedder`, which reports `enabled = False`. Recall checks
that flag and skips its vector channel, so the lean install — no extras, no API key,
no model download — still gets full-text and topic-key retrieval. Nothing about
memory requires an embedder; it only gets better with one.

Backends are registered rather than enumerated, the same way model providers are, so
a plugin can add one without core naming it. Every heavy import happens inside the
factory that needs it: `tests/unit/test_invariants.py` fails the build on an optional
dependency imported at module scope, and the `lean` CI job imports every module with
no extras installed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any, Protocol, runtime_checkable

from felix.config import Settings
from felix.timeouts import DEFAULT_CONNECT_TIMEOUT_S

logger = logging.getLogger("felix.memory.embedder")


# An embeddings call is a model-provider request, so it honours the same ceiling; the
# default stands in when a caller builds the embedder without settings.
DEFAULT_EMBED_TIMEOUT_S = 60.0


@runtime_checkable
class Embedder(Protocol):
    """Turns text into vectors.

    ``enabled`` is part of the contract rather than an implementation detail: callers
    branch on it to decide whether a vector channel is worth running at all, which is
    what lets the whole feature degrade instead of erroring.
    """

    enabled: bool
    dim: int | None

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class NullEmbedder:
    """The default. Recall degrades to full-text and topic-key channels."""

    enabled = False
    dim: int | None = None

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return []


class SentenceTransformersEmbedder:
    """Local embeddings via the optional ``embeddings`` extra.

    Delegates to :func:`felix.embeddings.encode_texts` rather than loading its own
    copy — that module already caches the model under a lock, and a second cache would
    mean a second multi-hundred-MB copy of the same weights resident.
    """

    enabled = True

    def __init__(self, model: str, dim: int | None = None) -> None:
        self.model = model
        self.dim = dim

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        from felix.embeddings import encode_texts_async

        vectors = await encode_texts_async(list(texts), model=self.model)
        if vectors is None:
            raise RuntimeError(
                f"embedding model {self.model!r} is unavailable — install the "
                "'embeddings' extra (felix-harness[embeddings]) or set "
                "FELIX_MEMORY_EMBEDDER=none"
            )
        if vectors and self.dim is None:
            self.dim = len(vectors[0])
        return vectors


class OpenAIEmbedder:
    """Any OpenAI-compatible ``/embeddings`` endpoint, which includes Ollama.

    Implemented straight on httpx, a core dependency, so this backend needs no extra.
    """

    enabled = True

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str,
        dim: int | None = None,
        timeout_s: float = DEFAULT_EMBED_TIMEOUT_S,
    ) -> None:
        self.model = model
        self.dim = dim
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        import httpx

        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        # Connect is pinned separately: raising the request ceiling for a large batch must
        # not also raise the ceiling on reaching a provider that is simply not there.
        timeout = httpx.Timeout(self._timeout_s, connect=DEFAULT_CONNECT_TIMEOUT_S)
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{self._base_url}/embeddings",
                json={"model": self.model, "input": list(texts)},
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json().get("data", [])
        # Returned in input order, but sort by index rather than trust that.
        ordered = sorted(data, key=lambda d: d.get("index", 0))
        vectors = [[float(x) for x in item["embedding"]] for item in ordered]
        if vectors and self.dim is None:
            self.dim = len(vectors[0])
        return vectors


EmbedderFactory = Callable[[Settings], Any]

_backends: dict[str, EmbedderFactory] = {}


def register_embedder_backend(name: str, factory: EmbedderFactory) -> None:
    _backends[name] = factory


def list_embedder_backends() -> list[str]:
    return sorted(_backends)


def _build_none(settings: Settings) -> Embedder:
    return NullEmbedder()


def _build_sentence_transformers(settings: Settings) -> Embedder:
    return SentenceTransformersEmbedder(
        model=str(getattr(settings, "memory_embedding_model", "") or "bge-base-en-v1.5"),
        dim=int(getattr(settings, "memory_embedding_dim", 0) or 0) or None,
    )


def _build_openai(settings: Settings) -> Embedder:
    # `openai_base_url` is not a field on Settings and never was, so this read always fell
    # through to the default: the embedder could not be pointed at the same gateway as the
    # model client, silently. Resolving through the provider descriptor means the two now
    # agree by construction, including a FELIX_MODEL_PROVIDER_OPTIONS override.
    from felix_ai.providers.compat import OPENAI_COMPATIBLE

    from felix.patterns.model import resolve_provider_config

    openai_spec = next(spec for spec in OPENAI_COMPATIBLE if spec.name == "openai")
    base_url, api_key, _headers = resolve_provider_config(openai_spec, settings)
    return OpenAIEmbedder(
        model=str(getattr(settings, "memory_embedding_model", "") or "text-embedding-3-small"),
        api_key=api_key,
        base_url=base_url,
        dim=int(getattr(settings, "memory_embedding_dim", 0) or 0) or None,
        timeout_s=float(getattr(settings, "model_timeout_seconds", DEFAULT_EMBED_TIMEOUT_S)),
    )


def _build_ollama(settings: Settings) -> Embedder:
    base = str(getattr(settings, "ollama_base_url", "") or "http://localhost:11434/v1")
    return OpenAIEmbedder(
        model=str(getattr(settings, "memory_embedding_model", "") or "nomic-embed-text"),
        api_key="",
        base_url=base,
        dim=int(getattr(settings, "memory_embedding_dim", 0) or 0) or None,
        timeout_s=float(getattr(settings, "model_timeout_seconds", DEFAULT_EMBED_TIMEOUT_S)),
    )


def _build_compat(provider_name: str) -> EmbedderFactory:
    """An embedder against any registered OpenAI-compatible provider.

    `/embeddings` is part of the same wire format as `/chat/completions`, so every provider
    in that table can back one — and it resolves through the same descriptor, which means
    the embedder and the model client cannot disagree about the endpoint or the credential.
    That disagreement is exactly what the phantom `settings.openai_base_url` caused.
    """

    def factory(settings: Settings) -> Embedder:
        from felix_ai.providers import builtin_provider_specs

        from felix.patterns.model import resolve_provider_config

        spec = next(s for s in builtin_provider_specs() if s.name == provider_name)
        base_url, api_key, _headers = resolve_provider_config(spec, settings)
        return OpenAIEmbedder(
            model=str(getattr(settings, "memory_embedding_model", "") or ""),
            api_key=api_key,
            base_url=base_url,
            dim=int(getattr(settings, "memory_embedding_dim", 0) or 0) or None,
            timeout_s=float(getattr(settings, "model_timeout_seconds", DEFAULT_EMBED_TIMEOUT_S)),
        )

    return factory


register_embedder_backend("none", _build_none)
register_embedder_backend("sentence_transformers", _build_sentence_transformers)
register_embedder_backend("openai", _build_openai)
register_embedder_backend("ollama", _build_ollama)


def _register_compat_embedders() -> None:
    """Every OpenAI-compatible provider becomes a selectable `FELIX_MEMORY_EMBEDDER`.

    `openai` and `ollama` keep their hand-written factories above because they carry model
    defaults worth keeping; the rest have no sensible default embedding model, so
    `FELIX_MEMORY_EMBEDDING_MODEL` is required for them.
    """
    from felix_ai.providers.compat import OPENAI_COMPATIBLE

    for spec in OPENAI_COMPATIBLE:
        if spec.name in _backends:
            continue
        register_embedder_backend(spec.name, _build_compat(spec.name))


_register_compat_embedders()


def build_embedder(settings: Settings) -> Embedder:
    """The configured embedder, or a no-op one.

    An unknown backend degrades to :class:`NullEmbedder` with a warning rather than
    failing startup: a typo in a setting should cost semantic recall, not the service.
    """
    name = str(getattr(settings, "memory_embedder", "none") or "none")
    factory = _backends.get(name)
    if factory is None:
        logger.warning(
            "unknown FELIX_MEMORY_EMBEDDER %r; recall will run full-text only (known: %s)",
            name,
            ", ".join(list_embedder_backends()),
        )
        return NullEmbedder()
    return factory(settings)


__all__ = [
    "DEFAULT_EMBED_TIMEOUT_S",
    "Embedder",
    "EmbedderFactory",
    "NullEmbedder",
    "OpenAIEmbedder",
    "SentenceTransformersEmbedder",
    "build_embedder",
    "list_embedder_backends",
    "register_embedder_backend",
]
