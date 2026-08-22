"""Workspace file tools sandboxed under ``Settings.workspace_root``."""

from __future__ import annotations

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

    query: str = Field(min_length=1, description="Literal or regex pattern to search for.")
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
            try:
                item["size"] = child.stat().st_size
            except OSError:
                pass
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


async def _search_files(args: SearchFilesArgs) -> str:
    try:
        root = _workspace_root()
        target = resolve_under_root(root, args.path)
    except ValueError as exc:
        return f"error: {exc}"
    if not target.exists():
        return f"error: not found: {args.path}"
    if target.is_file():
        files = [target]
    else:
        files = [p for p in target.rglob("*") if p.is_file()]

    pattern: re.Pattern[str] | None = None
    if args.regex:
        try:
            pattern = re.compile(args.query)
        except re.error as exc:
            return f"error: invalid regex: {exc}"

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
            matched = bool(pattern.search(line)) if pattern else args.query in line
            if matched:
                hits.append(
                    {
                        "path": str(path.relative_to(root)),
                        "line": i,
                        "text": line[:400],
                    }
                )
                if len(hits) >= args.max_hits:
                    break
    return json.dumps({"query": args.query, "hits": hits})


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
