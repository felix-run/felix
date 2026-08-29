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


def _build_compat(provider_name: str) -> EmbedderFactory:
    """An embedder against a registered OpenAI-compatible provider that serves `/embeddings`.

    Resolved through the same descriptor as the model client, so the two cannot disagree
    about the endpoint or the credential — the disagreement the phantom
    `settings.openai_base_url` caused, where the embedder silently always went to
    api.openai.com while the model client honoured the configured gateway.
    """

    def factory(settings: Settings) -> Embedder:
        from felix_ai.providers import builtin_provider_specs

        from felix.patterns.model import resolve_provider_config

        spec = next(s for s in builtin_provider_specs() if s.name == provider_name)
        base_url, api_key, _headers = resolve_provider_config(spec, settings)
        # Only an *explicitly set* FELIX_MEMORY_EMBEDDING_MODEL wins. The field's schema
        # default is `bge-base-en-v1.5`, a sentence-transformers name, and reading it
        # unconditionally sent that string to OpenAI as a model id — which the old
        # `_build_openai` did too, since its own default was unreachable behind a field
        # that is never empty. Same trick `runtime.py` uses for `context_window_tokens`.
        declared = "memory_embedding_model" in getattr(settings, "model_fields_set", set())
        configured = str(getattr(settings, "memory_embedding_model", "") or "")
        model = (configured if declared else "") or spec.embedding_model
        if not model:
            # An empty model id would go out as `{"model": ""}` and fail at the provider
            # with an error naming neither Felix nor the setting to fix.
            raise RuntimeError(
                f"FELIX_MEMORY_EMBEDDER={provider_name} needs FELIX_MEMORY_EMBEDDING_MODEL "
                f"— {provider_name} has no default embedding model."
            )
        return OpenAIEmbedder(
            model=model,
            api_key=api_key,
            base_url=base_url,
            dim=int(getattr(settings, "memory_embedding_dim", 0) or 0) or None,
            timeout_s=float(getattr(settings, "model_timeout_seconds", DEFAULT_EMBED_TIMEOUT_S)),
        )

    return factory


register_embedder_backend("none", _build_none)
register_embedder_backend("sentence_transformers", _build_sentence_transformers)


def _register_compat_embedders() -> None:
    """Register every provider that *says* it serves `/embeddings`.

    Registering the whole OpenAI-compatible table instead was wrong in a way that looked
    right: several of those endpoints implement chat only, so `FELIX_MEMORY_EMBEDDER=groq`
    passed the registry-backed startup validation and then failed at the first embed. The
    capability belongs on the row.
    """
    from felix_ai.providers import builtin_provider_specs

    for spec in builtin_provider_specs():
        if spec.supports_embeddings:
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
