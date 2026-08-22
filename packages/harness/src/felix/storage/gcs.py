"""Google Cloud Storage object store."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from felix.config import Settings


class GcsObjectStore:
    """GCS via google-cloud-storage (optional extra `gcp`)."""

    def __init__(self, settings: Settings) -> None:
        self._bucket_name = settings.gcs_bucket or settings.s3_bucket
        self._client = None
        self._bucket = None

    def _ensure(self) -> None:
        if self._client is None:
            try:
                from google.cloud import storage
            except ImportError as e:
                raise RuntimeError(
                    "GCS backend requires: uv sync --extra gcp (google-cloud-storage)"
                ) from e
            self._client = storage.Client()
            self._bucket = self._client.bucket(self._bucket_name)

    async def get(self, key: str) -> bytes | None:
        import asyncio

        self._ensure()
        assert self._bucket is not None

        def _get() -> bytes | None:
            blob = self._bucket.blob(key)
            if not blob.exists():
                return None
            return blob.download_as_bytes()

        return await asyncio.to_thread(_get)

    async def put(
        self, key: str, data: bytes, *, content_type: str = "application/octet-stream"
    ) -> None:
        import asyncio

        self._ensure()
        assert self._bucket is not None

        def _put() -> None:
            blob = self._bucket.blob(key)
            blob.upload_from_string(data, content_type=content_type)

        await asyncio.to_thread(_put)

    async def delete(self, key: str) -> None:
        import asyncio

        self._ensure()
        assert self._bucket is not None

        def _del() -> None:
            blob = self._bucket.blob(key)
            if blob.exists():
                blob.delete()

        await asyncio.to_thread(_del)

    async def exists(self, key: str) -> bool:
        import asyncio

        self._ensure()
        assert self._bucket is not None

        def _ex() -> bool:
            return bool(self._bucket.blob(key).exists())

        return await asyncio.to_thread(_ex)
