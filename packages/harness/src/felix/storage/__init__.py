"""Cloud-agnostic object storage — S3 / GCS / filesystem / memory.

Heavy SDKs live behind optional extras:
  - ``felix-harness[aws]`` → aiobotocore (S3 / MinIO)
  - ``felix-harness[gcp]`` → google-cloud-storage

Small VMs should prefer ``FELIX_OBJECT_STORE=fs`` (no extra deps).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


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
]
