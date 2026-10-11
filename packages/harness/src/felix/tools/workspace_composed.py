"""`glob` and `multi_edit`: workspace tools built from operations every backend already has.

Each workspace backend serves the same operations -- the local filesystem, and the hosted sandbox
through the gateway, whose helper is held to the local code as syntax trees. A tool composed from
those operations runs on both with no new gateway operation and nothing to keep in step:

- `glob` filters the backend's `tree` walk (which skips `.git` and the scopes directory and follows
  no link) by a pattern -- `**/*.py`, `src/**/test_*.py` -- so a model finds files by name without
  listing directories one at a time.
- `multi_edit` applies several exact-string edits to one file in order, all or none: it reads the
  file, applies them in memory with `edit_file`'s rules, and writes once with `write_file_checked`,
  which refuses if the file changed since the read. Locally the compare and the replace happen
  under one lock; on the hosted backend they are three gateway calls (`write_file_checked` says what
  that leaves open), as for the operator's file pane.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field

from felix.tools.types import ToolOutput
from felix.tools.workspace import (
    _MAX_READ_BYTES,
    _MAX_WRITE_BYTES,
    NotAFileError,
    _missing,
    _os_failed,
    _path_refused,
    _refuse,
    _seam,
    workspace_parts,
)
from felix.tools.workspace_backend import EditRefused, WorkspaceChanged

# Entries `glob` walks before it stops and says so: the tree is the whole scope, and a scope can
# hold a `node_modules`. Matches are capped separately, at what a model can read.
_GLOB_SCAN_LIMIT = 20_000
_MAX_GLOB_RESULTS = 500
_MAX_PATTERN_CHARS = 256
_MAX_EDITS = 50


class GlobArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pattern: str = Field(
        min_length=1,
        max_length=_MAX_PATTERN_CHARS,
        description="Glob relative to path: `*` within a name, `**` across folders, e.g. `**/*.py`.",
    )
    path: str = Field(default=".", description="Folder to search under, relative to the workspace root.")
    max_results: int = Field(default=100, ge=1, le=_MAX_GLOB_RESULTS)


class Edit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    old_string: str = Field(min_length=1, description="Exact text to find, after the edits before it.")
    new_string: str = Field(description="Text to put in its place. Empty deletes the match.")
    replace_all: bool = Field(default=False, description="Replace every occurrence.")


class MultiEditArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, description="File path relative to the workspace root.")
    edits: list[Edit] = Field(
        min_length=1,
        max_length=_MAX_EDITS,
        description="Applied in order, each to the result of the one before; all or none land.",
    )


def apply_edits(text: str, edits: list[Edit], path: str) -> tuple[str, int]:
    """`text` with every edit applied in order, and how many replacements they made. `EditRefused`
    for the first edit that `edit_file` would refuse: not found, identical, ambiguous without
    `replace_all`, or a result over `_MAX_WRITE_BYTES`."""
    total = 0
    for n, edit in enumerate(edits, 1):
        found = text.count(edit.old_string)
        if found == 0:
            raise EditRefused(f"edit {n}: old_string not found in {path}")
        if edit.old_string == edit.new_string:
            raise EditRefused(f"edit {n}: old_string and new_string are identical")
        if found > 1 and not edit.replace_all:
            raise EditRefused(
                f"edit {n}: old_string appears {found} times in {path} — extend it with surrounding "
                "lines until it is unique, or pass replace_all"
            )
        # Projected before `str.replace` builds it, as `edit_file` does: `replace_all` multiplies.
        grew = len(edit.new_string.encode("utf-8")) - len(edit.old_string.encode("utf-8"))
        projected = len(text.encode("utf-8")) + found * grew
        if projected > _MAX_WRITE_BYTES:
            raise EditRefused(
                f"edit {n}: the result would be {projected} bytes, over the {_MAX_WRITE_BYTES} limit"
            )
        text = text.replace(edit.old_string, edit.new_string)
        total += found
    return text, total


def _glob_relative(pattern: str) -> str | None:
    """Why `pattern` is refused, or None: it is matched against paths under `path`, so it may not
    climb out of it or start at the root."""
    parts = PurePosixPath(pattern).parts
    if pattern.startswith("/") or ".." in parts or "\0" in pattern:
        return "pattern must be relative to path, with no `..`"
    return None


async def _glob(args: GlobArgs) -> ToolOutput:
    backend, scope = _seam()
    try:
        await backend.prepare(scope)
        base = "/".join(workspace_parts(args.path))
    except ValueError as exc:
        return _path_refused(exc)
    refused = _glob_relative(args.pattern)
    if refused:
        return _refuse(refused)
    try:
        walked = await backend.tree(scope, _GLOB_SCAN_LIMIT)
    except ValueError as exc:
        return _path_refused(exc)
    except OSError as exc:
        return _os_failed(exc)
    matches: list[str] = []
    more = False
    prefix = f"{base}/" if base else ""
    for entry in walked.entries:
        rel = str(entry.get("path") or "")
        if entry.get("type") != "file" or not rel.startswith(prefix):
            continue
        # `full_match`: `**` spans folders, `*` stays within a name, the whole path must match.
        if PurePosixPath(rel[len(prefix) :]).full_match(args.pattern):
            if len(matches) == args.max_results:
                more = True
                break
            matches.append(rel)
    return json.dumps(
        {
            "pattern": args.pattern,
            "path": base or ".",
            "files": matches,
            # Either cap: more matches than max_results, or a tree too big to walk whole.
            "truncated": more or walked.truncated,
        }
    )


async def _multi_edit(args: MultiEditArgs) -> ToolOutput:
    backend, scope = _seam()
    try:
        await backend.prepare(scope)
        workspace_parts(args.path)
    except ValueError as exc:
        return _path_refused(exc)
    try:
        read = await backend.read_file(scope, args.path, 0, _MAX_READ_BYTES)
    except NotAFileError:
        return _refuse(f"not a file: {args.path}")
    except ValueError as exc:
        return _path_refused(exc)
    except OSError as exc:
        if _missing(exc):
            return _refuse(f"not a file: {args.path}")
        return _os_failed(exc)
    if read.size > len(read.data):
        return _refuse(f"{args.path} is over {_MAX_READ_BYTES} bytes; use edit_file one change at a time")
    try:
        text = read.data.decode("utf-8")
    except UnicodeDecodeError:
        return _refuse(f"not UTF-8 text: {args.path}")
    try:
        edited, replacements = apply_edits(text, args.edits, args.path)
    except EditRefused as exc:
        # Nothing was written: the edits before the refused one are discarded with it.
        return _refuse(f"{exc} (no edit was applied)")
    payload = edited.encode("utf-8")
    try:
        written = await backend.write_file_checked(
            scope, args.path, payload, hashlib.sha256(read.data).hexdigest()
        )
    except WorkspaceChanged:
        return _refuse(
            f"{args.path} changed while the edits were applied; read it again (no edit was applied)"
        )
    except ValueError as exc:
        return _path_refused(exc)
    except OSError as exc:
        return _os_failed(exc)
    return json.dumps(
        {"path": written.path, "edits": len(args.edits), "replacements": replacements, "bytes": written.bytes}
    )


__all__ = ["Edit", "GlobArgs", "MultiEditArgs", "apply_edits"]
