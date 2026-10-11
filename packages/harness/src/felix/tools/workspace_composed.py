"""`glob` and `multi_edit`: workspace tools built from operations every backend already has.

Each workspace backend serves the same operations -- the local filesystem, and the hosted sandbox
through the gateway, whose helper is held to the local code as syntax trees. A tool composed from
those operations runs on both with no new gateway operation:

- `glob` filters the backend's `tree` walk (which skips `.git` and the scopes directory and follows
  no link) by a pattern -- `**/*.py`, `src/**/test_*.py` -- so a model finds files by name without
  listing directories one at a time.
- `multi_edit` applies several exact-string edits to one file in order, all or none: it reads the
  file, applies them in memory, and writes once with `write_file_checked`, which refuses if the file
  changed since the read. `apply_edits` re-states `edit_file`'s rules for one string, since
  `LocalBackend.edit_file` cannot be refactored without drifting from the gateway's copy of it;
  `tests/unit/test_workspace_composed.py` holds the two to the same decisions. Locally the compare
  and the replace happen under the lock every tool write takes; on the hosted backend they are
  three gateway calls, ordered against this process's tool writes (`workspace_hosted`).
"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
import time

from pydantic import BaseModel, ConfigDict, Field

from felix.tools.errors import ToolErrorCode, tool_error_output
from felix.tools.provider import InMemoryToolProvider
from felix.tools.types import ToolOutput, define_tool
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
    writable_file,
)
from felix.tools.workspace_backend import EditRefused, WorkspaceChanged

# Entries `glob` walks before it stops and says so: the tree is the whole scope, and a scope can
# hold a `node_modules`. Matches are capped separately, at what a model can read.
_GLOB_SCAN_LIMIT = 20_000
_MAX_GLOB_RESULTS = 500
_MAX_PATTERN_CHARS = 256
# A pattern is matched a segment at a time with `fnmatch`, whose regex is atomic per `*` and so
# linear in a name; these bound the rest of the work. (`PurePath.full_match` compiles one
# backtracking regex for the whole path: `*a*a*a*a*a*a*b` against a long name ran for minutes.)
_MAX_GLOB_WILDCARDS = 8
_MAX_GLOB_DEEP = 4
_MAX_GLOB_SEGMENTS = 32
_GLOB_BUDGET_S = 5.0
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


class StringEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    old_string: str = Field(
        min_length=1,
        max_length=_MAX_WRITE_BYTES,
        description="Exact text to find, after the edits before it.",
    )
    new_string: str = Field(
        max_length=_MAX_WRITE_BYTES, description="Text to put in its place. Empty deletes it."
    )
    replace_all: bool = Field(default=False, description="Replace every occurrence.")


class MultiEditArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, description="File path relative to the workspace root.")
    edits: list[StringEdit] = Field(
        min_length=1,
        max_length=_MAX_EDITS,
        description="Applied in order, each to the result of the one before; all or none land.",
    )


def apply_edits(text: str, edits: list[StringEdit], path: str) -> tuple[str, int]:
    """`text` with every edit applied in order, and how many replacements they made.

    `EditRefused` for the first edit `edit_file`'s rules refuse -- not found, identical, ambiguous
    without `replace_all` -- with `edit_file`'s words behind an `edit N:` prefix, or for a result
    over `_MAX_WRITE_BYTES`: `multi_edit`'s own cap, where `edit_file`'s is 4 MB, because it reads
    and writes the whole file through the 512 KB read and checked-write windows.
    """
    total = 0
    for n, edit in enumerate(edits, 1):
        found = text.count(edit.old_string)
        if found == 0:
            raise EditRefused(f"edit {n}: old_string not found in {path}")
        if edit.old_string == edit.new_string:
            raise EditRefused(f"edit {n}: old_string and new_string are identical in {path}")
        if found > 1 and not edit.replace_all:
            raise EditRefused(
                f"edit {n}: old_string appears {found} times in {path} — extend it with "
                "surrounding lines until it is unique, or pass replace_all"
            )
        # Projected before `str.replace` builds it, as `edit_file` does: `replace_all` multiplies.
        grew = len(edit.new_string.encode("utf-8")) - len(edit.old_string.encode("utf-8"))
        projected = len(text.encode("utf-8")) + found * grew
        if projected > _MAX_WRITE_BYTES:
            raise EditRefused(
                f"edit {n}: the edit would make {path} {projected} bytes, over the {_MAX_WRITE_BYTES} limit"
            )
        text = text.replace(edit.old_string, edit.new_string)
        total += found
    return text, total


def _glob_pattern_refusal(pattern: str) -> str | None:
    """Why `pattern` is refused, or None. It is matched against paths under `path`, so it may not
    climb out of it or start at the root; and its wildcards are bounded, so no pattern costs more
    than a bounded amount per path."""
    segments = pattern.split("/")
    if pattern.startswith("/") or ".." in segments or "\0" in pattern:
        return "pattern must be relative to path, with no `..`"
    if len(segments) > _MAX_GLOB_SEGMENTS:
        return f"pattern has more than {_MAX_GLOB_SEGMENTS} segments"
    if sum(s == "**" for s in segments) > _MAX_GLOB_DEEP:
        return f"pattern has more than {_MAX_GLOB_DEEP} `**` segments"
    if sum(s.count("*") + s.count("?") for s in segments if s != "**") > _MAX_GLOB_WILDCARDS:
        return f"pattern has more than {_MAX_GLOB_WILDCARDS} `*` and `?` wildcards"
    return None


def glob_match(segments: list[str], parts: list[str]) -> bool:
    """Whether path `parts` match pattern `segments`: `**` is any number of whole folders, any other
    segment one name by `fnmatch` (case-sensitive). A table over (segment, part) positions, so the
    cost is their product, never exponential in the `**` count."""
    # reach[j]: the first i segments can match the first j parts.
    reach = [True] + [False] * len(parts)
    for seg in segments:
        nxt = [False] * (len(parts) + 1)
        if seg == "**":
            seen = False
            for j in range(len(parts) + 1):
                seen = seen or reach[j]
                nxt[j] = seen
        else:
            for j in range(1, len(parts) + 1):
                nxt[j] = reach[j - 1] and fnmatch.fnmatchcase(parts[j - 1], seg)
        if not any(nxt):
            return False  # no prefix of the path can match what is left
        reach = nxt
    return reach[len(parts)]


def _filter(
    entries: list[dict[str, object]], prefix: str, segments: list[str], limit: int, deadline: float
) -> tuple[list[str], bool, bool]:
    """`(matches, more, timed_out)`. Checks `deadline` itself: the thread `to_thread` runs it on
    cannot be cancelled, so a deadline only the caller kept would answer on time and leave the
    work running to the end."""
    matches: list[str] = []
    for n, entry in enumerate(entries):
        if n % 256 == 0 and time.monotonic() > deadline:
            return matches, False, True
        rel = str(entry.get("path") or "")
        if entry.get("type") != "file" or not rel.startswith(prefix):
            continue
        if glob_match(segments, rel[len(prefix) :].split("/")):
            if len(matches) == limit:
                return matches, True, False
            matches.append(rel)
    return matches, False, False


async def _glob(args: GlobArgs) -> ToolOutput:
    backend, scope = _seam()
    try:
        await backend.prepare(scope)
        base = "/".join(workspace_parts(args.path))
    except ValueError as exc:
        return _path_refused(exc)
    refused = _glob_pattern_refusal(args.pattern)
    if refused:
        return _refuse(refused)
    try:
        # The folder first: a typo in `path` is "not found", as for the other workspace tools,
        # not an empty list the model would read as "no such files".
        await backend.list_dir(scope, base or ".")
        walked = await backend.tree(scope, _GLOB_SCAN_LIMIT)
    except ValueError as exc:
        return _path_refused(exc)
    except OSError as exc:
        # First: `_missing` counts ENOTDIR as missing too.
        if isinstance(exc, NotADirectoryError):
            return _refuse(f"not a folder: {args.path}")
        if _missing(exc):
            return _refuse(f"not found: {args.path}")
        return _os_failed(exc)
    prefix = f"{base}/" if base else ""
    # Off the event loop, and stopping itself at the deadline: bounded per path, but over every
    # entry the walk returned.
    deadline = time.monotonic() + _GLOB_BUDGET_S
    matches, more, timed_out = await asyncio.to_thread(
        _filter, walked.entries, prefix, args.pattern.split("/"), args.max_results, deadline
    )
    if timed_out:
        return tool_error_output(
            ToolErrorCode.TIMEOUT, f"glob exceeded {_GLOB_BUDGET_S:.0f}s — narrow the pattern or path"
        )
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
        writable_file(args.path)
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
        # Never edit the window and write it back: that would cut the file to 512 KB.
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


def register_composed_tools(provider: InMemoryToolProvider) -> None:
    """Bind `glob` and `multi_edit` beside the workspace tools they are built from."""
    provider.register(
        "glob",
        lambda: define_tool(
            name="glob",
            replay_safe=True,
            read_only=True,
            description=(
                "Find workspace files by name pattern: `*` within a name, `**` across folders "
                "(`**/*.py`, `src/**/test_*.py`). Returns paths, not contents."
            ),
            args=GlobArgs,
            handler=_glob,
        ),
    )
    provider.register(
        "multi_edit",
        lambda: define_tool(
            name="multi_edit",
            description=(
                "Apply several exact-string edits to one workspace file, in order, each to the "
                "result of the one before. All land or none do. Use it for several changes to one "
                "file instead of repeated edit_file calls."
            ),
            args=MultiEditArgs,
            handler=_multi_edit,
        ),
    )


__all__ = ["GlobArgs", "MultiEditArgs", "StringEdit", "apply_edits", "glob_match", "register_composed_tools"]
