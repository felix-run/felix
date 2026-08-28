"""Cloud-agnostic object storage — S3 / GCS / filesystem / memory.

Heavy SDKs live behind optional extras:
  - ``felix-harness[aws]`` → aiobotocore (S3 / MinIO)
  - ``felix-harness[gcp]`` → google-cloud-storage

Small VMs should prefer ``FELIX_OBJECT_STORE=fs`` (no extra deps).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from felix.config import Settings

logger = logging.getLogger("felix.storage")


@runtime_checkable
class ObjectStore(Protocol):
    """Blob store for manifests, artifacts, workspace, audit warehouse."""

    async def get(self, key: str) -> bytes | None: ...
    async def put(self, key: str, data: bytes, *, content_type: str = "application/octet-stream") -> None: ...
    async def delete(self, key: str) -> None: ...
    async def exists(self, key: str) -> bool: ...


class MemoryObjectStore:
    """In-process store for unit tests."""

    def __init__(self) -> None:
        self._data: dict[str, bytes] = {}

    async def get(self, key: str) -> bytes | None:
        return self._data.get(key)

    async def put(self, key: str, data: bytes, *, content_type: str = "application/octet-stream") -> None:
        self._data[key] = data

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)

    async def exists(self, key: str) -> bool:
        return key in self._data


# One store per backend configuration, reused for the process lifetime.
#
# `build_object_store` was called inside `build_tenant_agent`, i.e. once per HTTP
# request, and `S3ObjectStore` opens an aiobotocore client it never closes — so every
# chat request leaked a client and its connection pool until the process hit EMFILE.
_STORE_CACHE: dict[tuple[Any, ...], ObjectStore] = {}


def _cache_key(settings: Any) -> tuple[Any, ...]:
    """Everything that changes which store a settings object describes."""
    return (
        getattr(settings, "object_store", ""),
        getattr(settings, "object_store_path", ""),
        getattr(settings, "data_dir", ""),
        getattr(settings, "s3_bucket", ""),
        getattr(settings, "s3_endpoint", ""),
        getattr(settings, "s3_region", ""),
        getattr(settings, "gcs_bucket", ""),
    )


def get_object_store(settings: object) -> ObjectStore:
    """Cached object store for these settings. Prefer this over `build_object_store`."""
    key = _cache_key(settings)
    store = _STORE_CACHE.get(key)
    if store is None:
        store = build_object_store(settings)
        _STORE_CACHE[key] = store
    return store


async def close_object_stores() -> None:
    """Release every cached store. Call on shutdown."""
    for store in list(_STORE_CACHE.values()):
        closer = getattr(store, "close", None)
        if closer is None:
            continue
        try:
            await closer()
        except Exception:
            logger.warning("object store close failed", exc_info=True)
    _STORE_CACHE.clear()


def reset_object_store_cache_for_tests() -> None:
    _STORE_CACHE.clear()


ObjectStoreFactory = Callable[["Settings"], ObjectStore]

_backends: dict[str, ObjectStoreFactory] = {}


def register_object_store(name: str, factory: ObjectStoreFactory) -> None:
    """Register an object-store backend for ``FELIX_OBJECT_STORE=<name>``.

    ``ObjectStore`` was already a Protocol, but the factory was a hardcoded
    if/elif, so a third party could implement the interface and still had no way
    to have it selected. Call this at import time from a ``felix.plugins`` entry
    point to add Azure Blob, a Vault-backed store, or a bespoke backend.
    """
    _backends[name] = factory


def list_object_stores() -> list[str]:
    return sorted(_backends)


def _build_memory(settings: Any) -> ObjectStore:
    _ = settings
    return MemoryObjectStore()


def _build_fs(settings: Any) -> ObjectStore:
    from felix.storage.fs import FilesystemObjectStore

    return FilesystemObjectStore(settings)


def _build_gcs(settings: Any) -> ObjectStore:
    from felix.storage.gcs import GcsObjectStore

    return GcsObjectStore(settings)


def _build_s3(settings: Any) -> ObjectStore:
    # requires felix-harness[aws] (aiobotocore)
    try:
        import aiobotocore  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "FELIX_OBJECT_STORE=s3 requires the aws extra "
            "(uv sync --extra aws) or switch to FELIX_OBJECT_STORE=fs for small VMs."
        ) from exc
    from felix.storage.s3 import S3ObjectStore

    return S3ObjectStore(settings)


register_object_store("memory", _build_memory)
register_object_store("fs", _build_fs)
register_object_store("gcs", _build_gcs)
register_object_store("s3", _build_s3)


def build_object_store(settings: object) -> ObjectStore:
    """Factory from FELIX_OBJECT_STORE (default s3). Backends are registrable.

    An unknown backend raises rather than degrading: objects are the artifact and
    skill substrate, so a typo must not silently strand writes in memory.
    """
    from felix.config import Settings

    assert isinstance(settings, Settings)
    backend = getattr(settings, "object_store", "s3")
    factory = _backends.get(backend)
    if factory is None:
        raise RuntimeError(
            f"Unknown FELIX_OBJECT_STORE={backend!r} (registered: {', '.join(list_object_stores())})"
        )
    return factory(settings)


__all__ = [
    "MemoryObjectStore",
    "ObjectStore",
    "ObjectStoreFactory",
    "build_object_store",
    "close_object_stores",
    "get_object_store",
    "list_object_stores",
    "register_object_store",
    "reset_object_store_cache_for_tests",
]
