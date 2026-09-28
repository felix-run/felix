"""Workspace file tools sandboxed under ``Settings.workspace_root``."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from felix.context import try_get_context
from felix.tools.errors import ToolErrorCode, tool_error_output
from felix.tools.provider import InMemoryToolProvider
from felix.tools.types import ToolOutput, ToolOutputDict, define_tool

_MAX_READ_BYTES = 512_000
_MAX_WRITE_BYTES = 512_000
# An in-place edit carries only the two strings, so the file it edits may be far larger
# than a model could write whole. CHANGELOG.md is 190 KiB and every pull request adds a
# paragraph to it; under `write_file` that is a 190 KiB round trip per entry.
_MAX_EDIT_FILE_BYTES = 4_000_000
_MAX_LIST_ENTRIES = 500
_MAX_SEARCH_HITS = 50
_MAX_SEARCH_FILE_BYTES = 256_000
# The pattern is model-supplied and compiled, so it is attacker-controlled in the
# prompt-injection sense. Python's `re` has no timeout, and a nested-quantifier
# pattern like (a+)+$ is exponential in the length of the line it is matched
# against — so bound the pattern, the line, and the wall-clock.
_MAX_QUERY_CHARS = 512
_MAX_SEARCH_LINE_CHARS = 4_000
_SEARCH_BUDGET_S = 5.0


class PathArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(default=".", description="Path relative to the workspace root.")


class ReadFileArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, description="File path relative to the workspace root.")
    offset: int = Field(default=0, ge=0, description="Byte offset to start reading.")
    limit: int = Field(
        default=_MAX_READ_BYTES,
        ge=1,
        le=_MAX_READ_BYTES,
        description="Max bytes to read.",
    )


class WriteFileArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, description="File path relative to the workspace root.")
    content: str = Field(description="UTF-8 text to write.")
    append: bool = Field(default=False, description="Append instead of overwrite.")


class EditFileArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, description="File path relative to the workspace root.")
    old_string: str = Field(
        min_length=1,
        description="Exact text to find. Include enough surrounding lines to appear once.",
    )
    new_string: str = Field(description="Text to put in its place. Empty deletes the match.")
    replace_all: bool = Field(
        default=False,
        description="Replace every occurrence instead of refusing an ambiguous match.",
    )


class SearchFilesArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(
        min_length=1,
        max_length=_MAX_QUERY_CHARS,
        description="Literal or regex pattern to search for.",
    )
    path: str = Field(default=".", description="Directory relative to the workspace root.")
    regex: bool = Field(default=False, description="Treat query as a regular expression.")
    max_hits: int = Field(default=20, ge=1, le=_MAX_SEARCH_HITS)


def resolve_under_root(root: Path, user_path: str) -> Path:
    """Resolve ``user_path`` under ``root``, rejecting escapes."""
    root_resolved = root.expanduser().resolve()
    raw = (user_path or ".").strip() or "."
    if Path(raw).is_absolute():
        raise ValueError("absolute paths are not allowed")
    target = (root_resolved / raw).resolve()
    if not target.is_relative_to(root_resolved):
        raise ValueError("path escapes workspace root")
    return target


def workspace_root() -> Path:
    """The checkout every workspace tool — and the shell tool — is confined to."""
    ctx = try_get_context()
    root = ""
    if ctx is not None:
        root = str(getattr(ctx.settings, "workspace_root", "") or "")
    if not root:
        raise ValueError("workspace_root is not configured (set FELIX_WORKSPACE_ROOT)")
    path = Path(root).expanduser().resolve()
    if not path.exists():
        raise ValueError(f"workspace_root does not exist: {path}")
    if not path.is_dir():
        raise ValueError(f"workspace_root is not a directory: {path}")
    return path


def _refuse(message: str) -> ToolOutputDict:
    """A call the model can fix by asking differently: bad path, missing file, ambiguous edit."""
    return tool_error_output(ToolErrorCode.INVALID_ARGUMENTS, message)


def _path_refused(exc: ValueError) -> ToolOutputDict:
    """A path that could not be resolved — the model's fault, unless no workspace exists at all.

    `workspace_root()` raises for an unconfigured or missing root and `resolve_under_root` for a
    path that escapes it. Only the second is something the model can correct, so the first is
    reported as the transport being unavailable rather than as a bad argument.
    """
    if "workspace_root" in str(exc):
        return tool_error_output(ToolErrorCode.TRANSPORT_UNAVAILABLE, str(exc))
    return _refuse(str(exc))


def _os_failed(exc: OSError) -> ToolOutputDict:
    """The filesystem refused. The one case the model cannot fix, and the one that was invisible.

    These used to be returned as plain `error: …` text, which carries no error marker, so the tool
    runner audited a write that failed with `Errno 13` as `tool_call` / `ok`, the metrics counted it
    as a success, and the eval trajectory — which reads the text against
    `FAILURE_CONTENT_PREFIXES` — did not count it at all. `tool_error_output` sets the marker the
    runner reads and the `[tool error/…]` prefix the trajectory reads, so all three agree.
    """
    code = ToolErrorCode.PERMISSION_DENIED if isinstance(exc, PermissionError) else ToolErrorCode.INTERNAL
    # Led by the exception's name, never by `str(exc)`. An `OSError` renders as `[Errno 13] …`,
    # and `tool_error_output` skips its prefix for text that already starts with `[` — so passing
    # the bare message produced a marked output whose *text* the trajectory still did not count.
    return tool_error_output(code, f"{type(exc).__name__}: {exc}")


async def _list_dir(args: PathArgs) -> ToolOutput:
    try:
        root = workspace_root()
        target = resolve_under_root(root, args.path)
    except ValueError as exc:
        return _path_refused(exc)
    if not target.exists():
        return _refuse(f"not found: {args.path}")
    if not target.is_dir():
        return _refuse(f"not a directory: {args.path}")
    entries: list[dict[str, Any]] = []
    for child in sorted(target.iterdir(), key=lambda p: p.name.lower()):
        if len(entries) >= _MAX_LIST_ENTRIES:
            break
        kind = "dir" if child.is_dir() else "file"
        rel = str(child.relative_to(root))
        item: dict[str, Any] = {"path": rel, "type": kind}
        if child.is_file():
            with contextlib.suppress(OSError):
                item["size"] = child.stat().st_size
        entries.append(item)
    return json.dumps({"path": str(target.relative_to(root)), "entries": entries})


async def _read_file(args: ReadFileArgs) -> ToolOutput:
    try:
        root = workspace_root()
        target = resolve_under_root(root, args.path)
    except ValueError as exc:
        return _path_refused(exc)
    if not target.exists() or not target.is_file():
        return _refuse(f"not a file: {args.path}")
    try:
        data = target.read_bytes()
    except OSError as exc:
        return _os_failed(exc)
    chunk = data[args.offset : args.offset + args.limit]
    try:
        text = chunk.decode("utf-8")
    except UnicodeDecodeError:
        return json.dumps(
            {
                "path": str(target.relative_to(root)),
                "offset": args.offset,
                "binary": True,
                "size": len(data),
                "bytes_read": len(chunk),
            }
        )
    return json.dumps(
        {
            "path": str(target.relative_to(root)),
            "offset": args.offset,
            "size": len(data),
            "content": text,
        }
    )


_write_locks: dict[str, asyncio.Lock] = {}


def _write_lock(target: Path) -> asyncio.Lock:
    """One lock per resolved path, so parallel tool calls cannot interleave on a file.

    `spec.tool_execution: parallel` runs a batch with `asyncio.gather`, and two calls in
    one batch can name the same file — directly, or by two paths that resolve to it
    through a symlink. Appends would interleave mid-write and a write racing an append
    would drop one of them. The key is the resolved path, so aliases share a lock; the
    map is process-local, which is the same scope as the writes it is ordering.
    """
    return _write_locks.setdefault(str(target), asyncio.Lock())


async def _write_file(args: WriteFileArgs) -> ToolOutput:
    try:
        root = workspace_root()
        target = resolve_under_root(root, args.path)
    except ValueError as exc:
        return _path_refused(exc)
    payload = args.content.encode("utf-8")
    if len(payload) > _MAX_WRITE_BYTES:
        return _refuse(f"content exceeds {_MAX_WRITE_BYTES} bytes")
    try:
        async with _write_lock(target):
            target.parent.mkdir(parents=True, exist_ok=True)
            if args.append and target.exists():
                with target.open("ab") as fh:
                    fh.write(payload)
            else:
                target.write_bytes(payload)
    except OSError as exc:
        return _os_failed(exc)
    return json.dumps(
        {
            "path": str(target.relative_to(root)),
            "bytes": len(payload),
            "append": args.append,
        }
    )


def _replace_file(target: Path, payload: bytes) -> None:
    """Write `payload` over `target` without ever leaving it half-written.

    `write_bytes` truncates first, so a failure partway leaves a file whose prior contents
    exist nowhere: an edit carries only the two strings, not the pre-image a whole-file write
    still has in its own arguments. The temporary file is a sibling, so the rename is atomic,
    and it inherits the target's mode — an edited `scripts/test.sh` that came back without its
    executable bit would be a strange way to break the gates.
    """
    tmp = target.with_name(f".{target.name}.felix-edit")
    try:
        tmp.write_bytes(payload)
        os.chmod(tmp, target.stat().st_mode)
        os.replace(tmp, target)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


async def _edit_file(args: EditFileArgs) -> ToolOutput:
    """Replace an exact string in a file, leaving every other byte where it was.

    Bytes in and bytes out, like `_read_file`: `read_text` would open in universal-newline
    mode and hand back `\n` for every `\r\n`, so writing the result would rewrite every line
    ending in a CRLF file that the edit never touched — and an `old_string` the model copied
    out of `read_file` would not match, because that tool preserves them.

    The lock is held across the read and the write. There is no `await` between them today,
    so within one event loop the body is already atomic and the lock cannot be observed to
    do anything; it is here because an edit is a read-modify-write, and the first person to
    move this I/O to a thread would otherwise have to notice that on their own.
    """
    try:
        root = workspace_root()
        target = resolve_under_root(root, args.path)
    except ValueError as exc:
        return _path_refused(exc)
    if not target.is_file():
        return _refuse(f"not a file: {args.path}")
    if len(args.new_string.encode("utf-8")) > _MAX_WRITE_BYTES:
        return _refuse(f"new_string exceeds {_MAX_WRITE_BYTES} bytes")
    try:
        async with _write_lock(target):
            size = target.stat().st_size
            if size > _MAX_EDIT_FILE_BYTES:
                return _refuse(f"{args.path} exceeds {_MAX_EDIT_FILE_BYTES} bytes")
            try:
                text = target.read_bytes().decode("utf-8")
            except UnicodeDecodeError:
                return _refuse(f"not UTF-8 text: {args.path}")
            found = text.count(args.old_string)
            if found == 0:
                return _refuse(f"old_string not found in {args.path}")
            if args.old_string == args.new_string:
                return _refuse(f"old_string and new_string are identical in {args.path}")
            if found > 1 and not args.replace_all:
                return _refuse(
                    f"old_string appears {found} times in {args.path} — extend it with "
                    "surrounding lines until it is unique, or pass replace_all"
                )
            # Both caps above bound an *input*, and `replace_all` multiplies them: the size of
            # what would be written is projected from the byte delta per match and refused
            # before `str.replace` builds it, because checking afterwards still allocates it.
            grew = len(args.new_string.encode("utf-8")) - len(args.old_string.encode("utf-8"))
            projected = size + found * grew
            if projected > _MAX_EDIT_FILE_BYTES:
                return _refuse(
                    f"the edit would make {args.path} {projected} bytes, over the "
                    f"{_MAX_EDIT_FILE_BYTES} limit"
                )
            # `found` is 1 unless replace_all said otherwise, so this replaces exactly the
            # matches the guards above allowed.
            payload = text.replace(args.old_string, args.new_string).encode("utf-8")
            _replace_file(target, payload)
    except OSError as exc:
        return _os_failed(exc)
    return json.dumps(
        {
            "path": str(target.relative_to(root)),
            "replacements": found,
            "bytes": len(payload),
        }
    )


# A quantified group that itself contains a quantifier — (a+)+, (a*)*, (\d+)* — is the
# construction that makes backtracking exponential. Python's `re` has no timeout and a
# worker thread cannot be killed, so the deadline below unblocks the *request* while the
# thread keeps burning CPU; repeated attempts would exhaust the pool. Rejecting the shape
# up front is the only part of this that actually stops the work.
#
# Detected by a linear scan, not a regex. The first version of this check *was* a regex
# with an ambiguous alternation, i.e. exactly the bug it exists to catch — CodeQL caught
# it. A scanner has no backtracking and is more precise about escapes and classes.
_QUANTIFIERS = frozenset("*+{")


def _reject_catastrophic(pattern: str) -> str | None:
    """Reason the pattern is refused, or None when it is acceptable."""
    # Stack entry: whether a quantifier has been seen at that group depth.
    stack: list[bool] = []
    i, n = 0, len(pattern)
    while i < n:
        ch = pattern[i]
        if ch == "\\":
            i += 2  # escaped char is a literal, quantifier or not
            continue
        if ch == "[":
            # Inside a character class, * + { and ) are literals.
            i += 1
            if i < n and pattern[i] == "^":
                i += 1
            if i < n and pattern[i] == "]":
                i += 1  # a leading ] is literal
            while i < n and pattern[i] != "]":
                i += 2 if pattern[i] == "\\" else 1
            i += 1
            continue
        if ch == "(":
            stack.append(False)
            i += 1
            continue
        if ch == ")":
            had_quantifier = stack.pop() if stack else False
            # A group that contained a quantifier makes its *parent* quantifier-bearing
            # too, so ((a+))+ is caught and not just (a+)+.
            if had_quantifier and stack:
                stack[-1] = True
            i += 1
            if had_quantifier and i < n and pattern[i] in _QUANTIFIERS:
                return (
                    "pattern nests a quantifier inside a quantified group (e.g. '(a+)+'), "
                    "which backtracks exponentially; rewrite it without the nesting"
                )
            continue
        if ch in _QUANTIFIERS and stack:
            stack[-1] = True
        i += 1
    return None


def _scan_files(
    files: list[Path],
    args: SearchFilesArgs,
    pattern: re.Pattern[str] | None,
    root: Path,
) -> list[dict[str, Any]]:
    """Synchronous scan, run on a worker thread under a deadline."""
    hits: list[dict[str, Any]] = []
    for path in files:
        if len(hits) >= args.max_hits:
            break
        try:
            if path.stat().st_size > _MAX_SEARCH_FILE_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            # Truncate before matching: backtracking cost grows with the length of the
            # subject, so an unbounded line is what makes a bad pattern expensive.
            subject = line[:_MAX_SEARCH_LINE_CHARS]
            matched = bool(pattern.search(subject)) if pattern else args.query in subject
            if matched:
                hits.append({"path": str(path.relative_to(root)), "line": i, "text": line[:400]})
                if len(hits) >= args.max_hits:
                    break
    return hits


async def _search_files(args: SearchFilesArgs) -> ToolOutput:
    try:
        root = workspace_root()
        target = resolve_under_root(root, args.path)
    except ValueError as exc:
        return _path_refused(exc)
    if not target.exists():
        return _refuse(f"not found: {args.path}")
    files = [target] if target.is_file() else [p for p in target.rglob("*") if p.is_file()]

    pattern: re.Pattern[str] | None = None
    if args.regex:
        refused = _reject_catastrophic(args.query)
        if refused:
            return _refuse(f"{refused}")
        try:
            pattern = re.compile(args.query)
        except re.error as exc:
            return _refuse(f"invalid regex: {exc}")

    def _scan() -> list[dict[str, Any]]:
        return _scan_files(files, args, pattern, root)

    try:
        # Off the event loop and on a deadline. `re` cannot be interrupted, so the thread
        # keeps burning CPU until it finishes — but the request returns and the API stays
        # responsive, which is the difference between a slow tool and a stalled process.
        hits = await asyncio.wait_for(asyncio.to_thread(_scan), _SEARCH_BUDGET_S)
        return json.dumps({"query": args.query, "hits": hits})
    except TimeoutError:
        return tool_error_output(
            ToolErrorCode.TIMEOUT,
            f"search exceeded {_SEARCH_BUDGET_S:.0f}s — narrow the pattern "
            "(a nested-quantifier regex can be exponential)",
        )


def register_workspace_tools(provider: InMemoryToolProvider) -> None:
    provider.register(
        "list_dir",
        lambda: define_tool(
            name="list_dir",
            replay_safe=True,
            description="List files and directories under the workspace root.",
            args=PathArgs,
            handler=_list_dir,
        ),
    )
    provider.register(
        "read_file",
        lambda: define_tool(
            name="read_file",
            replay_safe=True,
            description="Read a UTF-8 text file from the workspace.",
            args=ReadFileArgs,
            handler=_read_file,
        ),
    )
    provider.register(
        "write_file",
        lambda: define_tool(
            name="write_file",
            description="Write a UTF-8 text file in the workspace.",
            args=WriteFileArgs,
            handler=_write_file,
        ),
    )
    provider.register(
        "edit_file",
        lambda: define_tool(
            name="edit_file",
            description=(
                "Replace an exact string in a workspace file, leaving the rest of it untouched. "
                "Use this rather than write_file on any file you did not just create."
            ),
            args=EditFileArgs,
            handler=_edit_file,
        ),
    )
    provider.register(
        "search_files",
        lambda: define_tool(
            name="search_files",
            replay_safe=True,
            description="Search workspace files for a literal string or regex.",
            args=SearchFilesArgs,
            handler=_search_files,
        ),
    )


__all__ = [
    "EditFileArgs",
    "PathArgs",
    "ReadFileArgs",
    "SearchFilesArgs",
    "WriteFileArgs",
    "register_workspace_tools",
    "resolve_under_root",
    "workspace_root",
]
