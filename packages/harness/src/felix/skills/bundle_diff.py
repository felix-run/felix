"""A per-file diff of two skill bundles, bounded so a large one cannot make a large answer.

Used by the upstream check and update (`skills/upstream.py`): a file is `added`, `removed` or
`modified`, and unchanged files are left out. A text file carries a unified diff (`difflib`), cut at
a line boundary past a per-file cap and past what is left of a total; once the total is spent, a
text file is listed without one -- and the caller need not read it at all. A binary asset
(`binary.is_binary_asset_path`) carries its sizes only.

The text is whatever the bundle holds -- a third party's, for an import -- so the caller redacts it
on the way out, as a file read is, and a terminal strips its control characters.
"""

from __future__ import annotations

import difflib
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from felix.skills.binary import is_binary_asset_path

# A unified diff's characters: per file, and over one whole answer.
MAX_FILE_DIFF_CHARS = 16 * 1024
MAX_DIFF_CHARS = 128 * 1024


@dataclass(slots=True, frozen=True)
class Content:
    """One side of a changed file: its size in bytes, and its text -- None for a binary asset,
    which is reported by size alone, and for text that was not read."""

    size: int
    text: str | None = None

    @classmethod
    def of(cls, path: str, data: bytes) -> Content:
        """The bytes a library version or an import stores for ``path``: a binary asset's raw, a
        text file's UTF-8 (invalid sequences replaced, though a saved bundle holds none)."""
        if is_binary_asset_path(path):
            return cls(len(data))
        return cls(len(data), data.decode("utf-8", "replace"))


class DiffBuilder:
    """Collects the changed files of one answer; `add` each, then take the `result`."""

    def __init__(self, *, max_file_chars: int = MAX_FILE_DIFF_CHARS, max_total_chars: int = MAX_DIFF_CHARS):
        self._per_file = max_file_chars
        self._left = max_total_chars
        self._files: list[dict[str, Any]] = []
        self.truncated = False

    @property
    def exhausted(self) -> bool:
        """Whether the total is spent: a text file added now gets no diff, so need not be read."""
        return self._left <= 0

    def add(self, path: str, old: Content | None, new: Content | None) -> None:
        """A changed file: added (no ``old``), removed (no ``new``) or modified."""
        if old is None and new is None:
            raise ValueError(f"{path}: a change has at least one side")
        binary = is_binary_asset_path(path)
        diff, cut = (None, False) if binary else self._text(path, old, new)
        self._files.append(
            {
                "path": path,
                "change": "added" if old is None else "removed" if new is None else "modified",
                "binary": binary,
                "old_size": old.size if old is not None else None,
                "new_size": new.size if new is not None else None,
                "diff": diff,
                "truncated": cut,
            }
        )

    def _text(self, path: str, old: Content | None, new: Content | None) -> tuple[str | None, bool]:
        if self.exhausted or any(side is not None and side.text is None for side in (old, new)):
            self.truncated = True
            return None, True
        lines = difflib.unified_diff(
            old.text.splitlines(keepends=True) if old is not None and old.text else [],
            new.text.splitlines(keepends=True) if new is not None and new.text else [],
            fromfile=f"a/{path}" if old is not None else "/dev/null",
            tofile=f"b/{path}" if new is not None else "/dev/null",
        )
        text = "".join(line if line.endswith("\n") else f"{line}\n" for line in lines)
        cap = min(self._per_file, self._left)
        cut = len(text) > cap
        if cut:
            text = text[: text.rfind("\n", 0, cap) + 1 or cap]
            self.truncated = True
        self._left -= max(len(text), 1)
        return text, cut

    def result(self) -> dict[str, Any]:
        """``{files, diff_truncated}``, the files by path."""
        return {"files": sorted(self._files, key=lambda f: f["path"]), "diff_truncated": self.truncated}


def diff_bundles(old: Mapping[str, bytes], new: Mapping[str, bytes], **caps: int) -> dict[str, Any]:
    """Every file whose bytes differ between ``old`` and ``new`` (path → stored bytes)."""
    builder = DiffBuilder(**caps)
    for path in sorted(old.keys() | new.keys()):
        before, after = old.get(path), new.get(path)
        if before == after:
            continue
        builder.add(
            path,
            Content.of(path, before) if before is not None else None,
            Content.of(path, after) if after is not None else None,
        )
    return builder.result()


def git_blob_id(data: bytes, like: str) -> str:
    """The git object id of ``data`` as a blob, in the object format of the id ``like`` (SHA-1 for
    40 hex digits, SHA-256 for 64): what a tree names a file by, so a stored file can be compared
    with an upstream one without reading it."""
    digest = hashlib.sha1 if len(like) == 40 else hashlib.sha256
    return digest(f"blob {len(data)}\0".encode() + data, usedforsecurity=False).hexdigest()


__all__ = ["MAX_DIFF_CHARS", "MAX_FILE_DIFF_CHARS", "Content", "DiffBuilder", "diff_bundles", "git_blob_id"]
