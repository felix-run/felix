"""S3-compatible object store (AWS S3, MinIO, Cloudflare R2-as-S3 without Workers)."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from felix.config import Settings


class S3ObjectStore:
    def __init__(self, settings: Settings) -> None:
        self._endpoint = settings.s3_endpoint
        self._access_key = settings.s3_access_key
        self._secret_key = settings.s3_secret_key
        self._bucket = settings.s3_bucket
        self._region = settings.s3_region
        self._client = None

    async def _get_client(self):
        if self._client is None:
            try:
                from aiobotocore.session import get_session
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError(
                    "S3 object store requires felix-harness[aws] "
                    "(uv sync --extra aws). For small VMs use FELIX_OBJECT_STORE=fs."
                ) from exc

            session = get_session()
            kwargs: dict = {
                "region_name": self._region,
                "aws_access_key_id": self._access_key,
                "aws_secret_access_key": self._secret_key,
            }
            if self._endpoint:
                kwargs["endpoint_url"] = self._endpoint
            self._cm = session.create_client("s3", **kwargs)
            self._client = await self._cm.__aenter__()
        return self._client

    async def get(self, key: str) -> bytes | None:
        client = await self._get_client()
        try:
            resp = await client.get_object(Bucket=self._bucket, Key=key)
            async with resp["Body"] as stream:
                return await stream.read()
        except client.exceptions.NoSuchKey:
            return None
        except Exception as exc:
            if "NoSuchKey" in type(exc).__name__ or "404" in str(exc):
                return None
            raise

    async def put(
        self, key: str, data: bytes, *, content_type: str = "application/octet-stream"
    ) -> None:
        client = await self._get_client()
        await client.put_object(
            Bucket=self._bucket, Key=key, Body=data, ContentType=content_type
        )

    async def delete(self, key: str) -> None:
        client = await self._get_client()
        await client.delete_object(Bucket=self._bucket, Key=key)

    async def exists(self, key: str) -> bool:
        client = await self._get_client()
        try:
            await client.head_object(Bucket=self._bucket, Key=key)
            return True
        except Exception:
            return False
