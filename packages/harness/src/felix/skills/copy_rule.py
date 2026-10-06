"""The copy rule: whether an agent's skill save carries a file of imported text.

An agent can read an imported skill and write its text into a new one under another name; the
copy is as much a third party's as the original, so the save carries the import's lineage
(`library._lineage_import`). This module decides what counts as the same file.

Two digests per file, compared like with like and never one against the other:

* **bytes** (`file_digest`, `skill_file.sha256`) -- every file whose text is not empty or
  whitespace, and every non-empty binary asset. An exact copy matches however short it is: a
  one-line `curl ... | sh` is short and still someone else's.
* **normalized text** (`normalized_digest`, `skill_file.normalized_sha256`) -- text only, after
  `normalized_text`, and only at `COPY_FLOOR_CHARS` or more. A copy that only re-spaces,
  re-cases, swaps compatibility forms or inserts invisible format characters still matches. For
  `SKILL.md` it covers the body alone: the frontmatter carries the skill's `name`, so a SKILL.md
  copied under a new name would otherwise never match.

What it cannot see: a paraphrase, a partial copy, and any change of a non-whitespace character
(a look-alike letter from another script included). It compares text, not meaning.

**Stored format.** Both digests are written to `skill_file` when a version is saved and compared
later against digests computed here. Changing how either is computed -- the normalization, the
SKILL.md body rule, the encoding -- needs a migration that re-hashes the stored rows; rows written
the old way would otherwise silently stop matching.
"""

from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Mapping

from felix.skills.binary import decode_base64, is_binary_asset_path
from felix.skills.format import split_frontmatter

# The normalized digest is not compared for text shorter than this: under 32 characters of
# normalized text is the boilerplate many skills share -- `[]`, a license id, a heading, a coding
# line -- and too short to tell a copy from a coincidence once case and spacing are ignored. The
# byte digest still matches such a file exactly.
COPY_FLOOR_CHARS = 32


def stored_bytes(path: str, content: str) -> bytes:
    """What the object store holds for one bundle file: a binary asset arrives base64 (the bundle
    is text), everything else as UTF-8."""
    return decode_base64(content) if is_binary_asset_path(path) else content.encode("utf-8")


def digest(data: bytes) -> str:
    """The one digest spelling: lowercase hex sha256."""
    return hashlib.sha256(data).hexdigest()


def file_digest(path: str, content: str) -> str:
    """The byte digest of one bundle file, as `skill_file.sha256` records it."""
    return digest(stored_bytes(path, content))


def normalized_text(text: str) -> str:
    """``text`` as the copy rule compares it: Unicode NFKC, every format character (category `Cf`:
    zero-width space and joiners, the soft hyphen, the BOM, bidi controls) removed, casefolded,
    every run of whitespace one space, stripped."""
    folded = unicodedata.normalize("NFKC", text)
    visible = "".join(c for c in folded if unicodedata.category(c) != "Cf")
    return " ".join(visible.casefold().split())


def _compared_text(path: str, content: str) -> str:
    """The text a file's normalized digest covers: a SKILL.md's body without its frontmatter
    (split as the loader splits it), any other file whole."""
    if path == "SKILL.md":
        parts = split_frontmatter(content.lstrip("﻿"))
        if parts is not None:
            return parts[1]
    return content


def normalized_digest(path: str, content: str) -> str | None:
    """`skill_file.normalized_sha256`: the digest of the file's `normalized_text`; None for a
    binary asset."""
    if is_binary_asset_path(path):
        return None
    return digest(normalized_text(_compared_text(path, content)).encode("utf-8"))


def copy_digests(
    files: Mapping[str, str], *, inherited: Mapping[str, str] | None = None
) -> tuple[set[str], set[str]]:
    """The byte and normalized digests the copy rule looks up for ``files``.

    Left out: an empty or whitespace-only file (it matches every other one), a normalized digest
    under `COPY_FLOOR_CHARS`, and every file byte-identical to ``inherited``'s file at the same
    path -- the version this save edits, when that version carries no imported text: a file it
    keeps unchanged is not a copy, it was already vouched for there."""
    exact: set[str] = set()
    normalized: set[str] = set()
    for path, content in files.items():
        data = stored_bytes(path, content)
        byte_digest = digest(data)
        if inherited is not None and inherited.get(path) == byte_digest:
            continue
        if is_binary_asset_path(path):
            if data:
                exact.add(byte_digest)
            continue
        if not normalized_text(content):
            continue
        exact.add(byte_digest)
        compared = normalized_text(_compared_text(path, content))
        if len(compared) >= COPY_FLOOR_CHARS:
            normalized.add(digest(compared.encode("utf-8")))
    return exact, normalized


__all__ = [
    "COPY_FLOOR_CHARS",
    "copy_digests",
    "digest",
    "file_digest",
    "normalized_digest",
    "normalized_text",
    "stored_bytes",
]
