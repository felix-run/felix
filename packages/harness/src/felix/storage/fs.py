"""Filesystem object store — zero-deps backend for small VMs / local Docker."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from felix.config import Settings


# A key segment, allowlisted rather than screened for the bad cases. `..` was the one
# being checked for before, which left every other way a segment can be not-a-name —
# a newline, a NUL, a leading dash, an RTL override — accepted and handed to the
# filesystem. The lookahead is what excludes `.` and `..` while still allowing the
# dotfiles that instruction-file keys may legitimately name.
_SAFE_SEGMENT = re.compile(r"\A(?!\.\.?\Z)[A-Za-z0-9._-]+\Z")


class FilesystemObjectStore:
    """Local directory blob store (FELIX_OBJECT_STORE=fs)."""

    def __init__(self, settings: Settings) -> None:
        root = Path(getattr(settings, "object_store_path", "") or settings.data_dir) / "objects"
        root.mkdir(parents=True, exist_ok=True)
        self._root = root

    def _path(self, key: str) -> Path:
        """Where a key lives under the root, or `ValueError` if it cannot say.

        Every segment is validated *before* any of it reaches a path expression, so
        the path is built from names already known to be names. The containment check
        below is kept as a second, independent answer: the allowlist is a claim about
        what the key contains, and `relative_to` is a fact about where the result
        landed — symlinks included, which no amount of string checking can see.
        """
        segments = [s for s in key.strip("/").split("/") if s]
        if not segments or not all(_SAFE_SEGMENT.match(s) for s in segments):
            raise ValueError(f"invalid object key: {key!r}")
        path = self._root.joinpath(*segments).resolve()
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

    async def put(self, key: str, data: bytes, *, content_type: str = "application/octet-stream") -> None:
        _ = content_type
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    async def delete(self, key: str) -> None:
        path = self._path(key)
        path.unlink(missing_ok=True)

    async def exists(self, key: str) -> bool:
        return self._path(key).is_file()
