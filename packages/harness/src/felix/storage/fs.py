"""Filesystem object store — zero-deps backend for small VMs / local Docker."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from felix.config import Settings


class FilesystemObjectStore:
    """Local directory blob store (FELIX_OBJECT_STORE=fs)."""

    def __init__(self, settings: Settings) -> None:
        root = Path(getattr(settings, "object_store_path", "") or settings.data_dir) / "objects"
        root.mkdir(parents=True, exist_ok=True)
        self._root = root

    def _path(self, key: str) -> Path:
        # Prevent path traversal while preserving nested keys.
        safe = Path(key).as_posix().lstrip("/")
        if not safe or ".." in Path(safe).parts:
            raise ValueError(f"invalid object key: {key!r}")
        path = (self._root / safe).resolve()
        try:
            path.relative_to(self._root.resolve())
        except ValueError as exc:
            raise ValueError(f"invalid object key: {key!r}") from exc
        return path

    async def get(self, key: str) -> bytes | None:
        path = self._path(key)
        if not path.is_file():
            return None
        return path.read_bytes()

    async def put(
        self, key: str, data: bytes, *, content_type: str = "application/octet-stream"
    ) -> None:
        _ = content_type
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    async def delete(self, key: str) -> None:
        path = self._path(key)
        path.unlink(missing_ok=True)

    async def exists(self, key: str) -> bool:
        return self._path(key).is_file()
