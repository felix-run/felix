"""Workspace file tools sandboxed under ``Settings.workspace_root``."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from felix.context import try_get_context
from felix.tools.provider import InMemoryToolProvider
from felix.tools.types import define_tool

_MAX_READ_BYTES = 512_000
_MAX_WRITE_BYTES = 512_000
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


def _workspace_root() -> Path:
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


async def _list_dir(args: PathArgs) -> str:
    try:
        root = _workspace_root()
        target = resolve_under_root(root, args.path)
    except ValueError as exc:
        return f"error: {exc}"
    if not target.exists():
        return f"error: not found: {args.path}"
    if not target.is_dir():
        return f"error: not a directory: {args.path}"
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


async def _read_file(args: ReadFileArgs) -> str:
    try:
        root = _workspace_root()
        target = resolve_under_root(root, args.path)
    except ValueError as exc:
        return f"error: {exc}"
    if not target.exists() or not target.is_file():
        return f"error: not a file: {args.path}"
    try:
        data = target.read_bytes()
    except OSError as exc:
        return f"error: {exc}"
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


async def _write_file(args: WriteFileArgs) -> str:
    try:
        root = _workspace_root()
        target = resolve_under_root(root, args.path)
    except ValueError as exc:
        return f"error: {exc}"
    payload = args.content.encode("utf-8")
    if len(payload) > _MAX_WRITE_BYTES:
        return f"error: content exceeds {_MAX_WRITE_BYTES} bytes"
    try:
        async with _write_lock(target):
            target.parent.mkdir(parents=True, exist_ok=True)
            if args.append and target.exists():
                with target.open("ab") as fh:
                    fh.write(payload)
            else:
                target.write_bytes(payload)
    except OSError as exc:
        return f"error: {exc}"
    return json.dumps(
        {
            "path": str(target.relative_to(root)),
            "bytes": len(payload),
            "append": args.append,
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


async def _search_files(args: SearchFilesArgs) -> str:
    try:
        root = _workspace_root()
        target = resolve_under_root(root, args.path)
    except ValueError as exc:
        return f"error: {exc}"
    if not target.exists():
        return f"error: not found: {args.path}"
    files = [target] if target.is_file() else [p for p in target.rglob("*") if p.is_file()]

    pattern: re.Pattern[str] | None = None
    if args.regex:
        refused = _reject_catastrophic(args.query)
        if refused:
            return f"error: {refused}"
        try:
            pattern = re.compile(args.query)
        except re.error as exc:
            return f"error: invalid regex: {exc}"

    def _scan() -> list[dict[str, Any]]:
        return _scan_files(files, args, pattern, root)

    try:
        # Off the event loop and on a deadline. `re` cannot be interrupted, so the thread
        # keeps burning CPU until it finishes — but the request returns and the API stays
        # responsive, which is the difference between a slow tool and a stalled process.
        hits = await asyncio.wait_for(asyncio.to_thread(_scan), _SEARCH_BUDGET_S)
        return json.dumps({"query": args.query, "hits": hits})
    except TimeoutError:
        return (
            f"error: search exceeded {_SEARCH_BUDGET_S:.0f}s — narrow the pattern "
            "(a nested-quantifier regex can be exponential)"
        )


def register_workspace_tools(provider: InMemoryToolProvider) -> None:
    provider.register(
        "list_dir",
        lambda: define_tool(
            name="list_dir",
            description="List files and directories under the workspace root.",
            args=PathArgs,
            handler=_list_dir,
        ),
    )
    provider.register(
        "read_file",
        lambda: define_tool(
            name="read_file",
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
        "search_files",
        lambda: define_tool(
            name="search_files",
            description="Search workspace files for a literal string or regex.",
            args=SearchFilesArgs,
            handler=_search_files,
        ),
    )


__all__ = [
    "PathArgs",
    "ReadFileArgs",
    "SearchFilesArgs",
    "WriteFileArgs",
    "register_workspace_tools",
    "resolve_under_root",
]
