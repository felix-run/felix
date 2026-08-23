"""Cloud-agnostic object storage — S3 / GCS / filesystem / memory.

Heavy SDKs live behind optional extras:
  - ``felix-harness[aws]`` → aiobotocore (S3 / MinIO)
  - ``felix-harness[gcp]`` → google-cloud-storage

Small VMs should prefer ``FELIX_OBJECT_STORE=fs`` (no extra deps).
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

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


def build_object_store(settings: object) -> ObjectStore:
    """Factory from FELIX_OBJECT_STORE=s3|gcs|fs|memory (default s3)."""
    from felix.config import Settings

    assert isinstance(settings, Settings)
    backend = getattr(settings, "object_store", "s3")
    if backend == "memory":
        return MemoryObjectStore()
    if backend == "fs":
        from felix.storage.fs import FilesystemObjectStore

        return FilesystemObjectStore(settings)
    if backend == "gcs":
        from felix.storage.gcs import GcsObjectStore

        return GcsObjectStore(settings)
    # s3 — requires felix-harness[aws] (aiobotocore)
    try:
        import aiobotocore  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "FELIX_OBJECT_STORE=s3 requires the aws extra "
            "(uv sync --extra aws) or switch to FELIX_OBJECT_STORE=fs for small VMs."
        ) from exc
    from felix.storage.s3 import S3ObjectStore

    return S3ObjectStore(settings)


__all__ = [
    "MemoryObjectStore",
    "ObjectStore",
    "build_object_store",
    "close_object_stores",
    "get_object_store",
    "reset_object_store_cache_for_tests",
]
