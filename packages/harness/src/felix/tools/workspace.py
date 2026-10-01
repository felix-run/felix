"""Workspace file tools sandboxed under ``Settings.workspace_root``."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import os
import re
import secrets
import stat
import time
from collections.abc import Iterator
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
# A tree walk keeps one descriptor open per level it is inside, so the depth is bounded, and so
# is how much of one directory is read before it is sorted: a directory of a million entries
# would otherwise be listed whole to return the first 500, or to find the first 20 hits.
_MAX_SEARCH_DEPTH = 64
_MAX_DIR_BATCH = 10_000
# The temporary sibling an edit writes before renaming it over the target. Random, so a
# directory or link planted under a predictable name cannot block every edit of a file, and
# short and fixed-length, so a leaf near NAME_MAX still has a temporary name that fits.
_EDIT_TMP_PREFIX = ".felix-edit-"


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
    """Resolve ``user_path`` under ``root``, rejecting escapes.

    A *check*, not a way to open anything: it follows symlinks to decide, so the answer is
    stale the moment another process swaps a component. Every opener goes through
    `open_workspace_parent` instead.
    """
    root_resolved = root.expanduser().resolve()
    raw = (user_path or ".").strip() or "."
    if Path(raw).is_absolute():
        raise ValueError("absolute paths are not allowed")
    target = (root_resolved / raw).resolve()
    if not target.is_relative_to(root_resolved):
        raise ValueError("path escapes workspace root")
    return target


# --- opening a workspace path without following a symlink ---------------------------------
#
# The workspace is writable by code the agent runs — on the builder stack from another
# container, where a `setsid` loop can outlive any tool call. Check-then-open by name loses to
# it: between `resolve()` deciding `src/x` is inside the workspace and `open("src/x")`, `src`
# becomes a symlink to `/proc/self` and the API reads its own `environ`. So no workspace path
# is ever opened by name from the root. The walk below opens the root, then each component
# relative to the directory before it with `O_NOFOLLOW`, and the caller operates on the
# descriptor it ends with. A component swapped after it was opened is irrelevant (the
# descriptor names the inode, not the path); one swapped before is a symlink, and refused.
# `openat` with `O_NOFOLLOW` and `O_DIRECTORY` behaves the same on Linux and macOS, which is
# why this is not `openat2(RESOLVE_NO_SYMLINKS)` (Linux-only, and not in `os`).

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY


class SymlinkRefusedError(ValueError):
    """A workspace path with a symlink in it. Refused the way an escaping path is."""

    def __init__(self, shown: str) -> None:
        super().__init__(f"symlinks are not followed in workspace paths: {shown}")


def workspace_parts(user_path: str) -> list[str]:
    """``user_path`` as plain components under the root, or ``ValueError``.

    `..` is applied lexically, which is exact here: with no symlink ever followed, the parent
    of a component is the directory the walk came from.
    """
    raw = (user_path or ".").strip() or "."
    if Path(raw).is_absolute():
        raise ValueError("absolute paths are not allowed")
    parts: list[str] = []
    for seg in raw.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if not parts:
                raise ValueError("path escapes workspace root")
            parts.pop()
            continue
        if "\0" in seg:
            raise ValueError("path contains a NUL byte")
        parts.append(seg)
    return parts


def open_at(dir_fd: int, name: str, flags: int, shown: str, mode: int = 0o600) -> int:
    """`openat(dir_fd, name)` that never follows a symlink — the only way a name is opened.

    `O_NOFOLLOW` makes the kernel refuse a symlink as the final (and only) component; the
    errno for that differs (Linux `ELOOP`, or `ENOTDIR` beside `O_DIRECTORY`; BSDs `EMLINK`),
    so the failure is classified by an `lstat` afterwards. That lstat only chooses the
    message: the refusal itself already happened in the open. `ELOOP` needs no lstat — for a
    single component under `O_NOFOLLOW` it can only mean a link — and must not get one: a link
    swapped back for a file between the open and the lstat would be reported as an internal
    error instead of the refusal it was.
    """
    try:
        return os.open(name, flags | os.O_NOFOLLOW | os.O_CLOEXEC, mode, dir_fd=dir_fd)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise SymlinkRefusedError(shown) from None
        try:
            is_link = stat.S_ISLNK(os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode)
        except OSError:
            is_link = False
        if is_link:
            raise SymlinkRefusedError(shown) from None
        raise


def _open_root(root: Path) -> int:
    """The workspace root, opened as a directory and never through a symlink.

    The root is the operator's (FELIX_WORKSPACE_ROOT), and `workspace_root()` already refuses
    one configured as a link. This holds the line at the open itself, for a root that is
    swapped for a link afterwards and for callers that pass a root they did not get from there.
    Components *above* the root may be links (`/tmp` on macOS is one); only the root is checked.
    """
    try:
        return os.open(root, _DIR_FLAGS | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        if os.path.islink(root):
            raise ValueError(f"workspace_root is a symlink: {root}") from None
        raise


@contextlib.contextmanager
def open_workspace_parent(
    root: Path, user_path: str, *, create: bool = False
) -> Iterator[tuple[int, str | None, str]]:
    """Walk to the directory holding ``user_path``'s last component, following no symlink.

    Yields ``(dir_fd, leaf, rel)``: a descriptor of that directory (closed on exit), the last
    component (None when the path is the root itself), and the path relative to the root as
    the tools report it. ``create`` makes missing directories on the way, like `mkdir -p`.
    Raises ``ValueError`` for an escaping or absolute path or a symlink component, and
    ``OSError`` (`FileNotFoundError`, `NotADirectoryError`) for one that is not there.
    """
    parts = workspace_parts(user_path)
    fd = _open_root(root)
    try:
        for i, seg in enumerate(parts[:-1]):
            if create:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(seg, dir_fd=fd)
            nxt = open_at(fd, seg, _DIR_FLAGS, "/".join(parts[: i + 1]))
            os.close(fd)
            fd = nxt
        yield fd, (parts[-1] if parts else None), "/".join(parts) or "."
    finally:
        os.close(fd)


@contextlib.contextmanager
def open_workspace_dir(root: Path, user_path: str) -> Iterator[tuple[int, str]]:
    """``(dir_fd, rel)`` for a workspace directory, opened without following a symlink."""
    with open_workspace_parent(root, user_path) as (parent, leaf, rel):
        fd = os.dup(parent) if leaf is None else open_at(parent, leaf, _DIR_FLAGS, rel)
        try:
            yield fd, rel
        finally:
            os.close(fd)


class NotAFileError(ValueError):
    """The path names something other than a regular file — a directory, a FIFO, a device."""


def open_regular(dir_fd: int, name: str, flags: int, shown: str, mode: int = 0o600) -> int:
    """`open_at` that also refuses anything but a regular file.

    `O_NONBLOCK` so a FIFO the agent left in the workspace cannot park the open forever; it
    changes nothing for a regular file.
    """
    fd = open_at(dir_fd, name, flags | os.O_NONBLOCK, shown, mode)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise NotAFileError(shown)
    return fd


def _pread(fd: int, size: int, offset: int) -> bytes:
    """At most `size` bytes of `fd` from `offset` — never more, whatever the file's size."""
    chunks: list[bytes] = []
    while size > 0:
        got = os.pread(fd, size, offset)
        if not got:
            break
        chunks.append(got)
        size -= len(got)
        offset += len(got)
    return b"".join(chunks)


def _dir_batch(fd: int, *, dirs_and_files_only: bool = False) -> list[tuple[str, os.stat_result]]:
    """Up to `_MAX_DIR_BATCH` entries of the directory `fd` as `(name, lstat)`, unsorted.

    `scandir` over the descriptor, not `listdir` and a sort: the read stops at the batch, so a
    directory of millions of entries costs the same as one of ten thousand. Ordering is the
    tools' contract (a listing and a search are reproducible), so callers sort the batch —
    which means a directory past it shows the first `_MAX_DIR_BATCH` entries in *directory*
    order, sorted, rather than the lexically first ones. Nothing is followed: the stat is the
    entry's own (`follow_symlinks=False`).
    """
    out: list[tuple[str, os.stat_result]] = []
    with os.scandir(fd) as it:
        for entry in it:
            if len(out) >= _MAX_DIR_BATCH:
                break
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue  # vanished between the read and the stat
            if dirs_and_files_only and not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)):
                continue
            out.append((entry.name, st))
    return out


def _by_name(entry: tuple[str, os.stat_result]) -> str:
    return entry[0]


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        view = view[os.write(fd, view) :]


def workspace_root() -> Path:
    """The checkout every workspace tool — and the shell tool — is confined to."""
    ctx = try_get_context()
    root = ""
    if ctx is not None:
        root = str(getattr(ctx.settings, "workspace_root", "") or "")
    if not root:
        raise ValueError("workspace_root is not configured (set FELIX_WORKSPACE_ROOT)")
    configured = Path(root).expanduser()
    # The root itself may not be a link: whoever can repoint it moves every tool to another
    # directory. Components above it may be (`/tmp` on macOS), and are resolved below.
    if configured.is_symlink():
        raise ValueError(f"workspace_root is a symlink: {configured}")
    path = configured.resolve()
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


def _missing(exc: OSError) -> bool:
    """Not there, or a component on the way is a file: the model asked for something absent."""
    return isinstance(exc, FileNotFoundError | NotADirectoryError)


def _child_rel(rel: str, name: str) -> str:
    return name if rel == "." else f"{rel}/{name}"


async def _list_dir(args: PathArgs) -> ToolOutput:
    try:
        root = workspace_root()
        with open_workspace_dir(root, args.path) as (fd, rel):
            entries: list[dict[str, Any]] = []
            # Case-folded, then the exact name, which is unique in a directory: `A` and `a`
            # cannot swap places with the cut at `_MAX_LIST_ENTRIES` between them.
            batch = sorted(_dir_batch(fd), key=lambda e: (e[0].lower(), e[0]))
            for name, st in batch[:_MAX_LIST_ENTRIES]:
                mode = st.st_mode
                # A symlink is reported as one, never as what it points at: the tools will
                # not follow it, so calling it a file or a directory would be a lie.
                kind = "dir" if stat.S_ISDIR(mode) else "symlink" if stat.S_ISLNK(mode) else "file"
                item: dict[str, Any] = {"path": _child_rel(rel, name), "type": kind}
                if stat.S_ISREG(mode):
                    item["size"] = st.st_size
                entries.append(item)
    except ValueError as exc:
        return _path_refused(exc)
    except OSError as exc:
        if isinstance(exc, FileNotFoundError):
            return _refuse(f"not found: {args.path}")
        if isinstance(exc, NotADirectoryError):
            return _refuse(f"not a directory: {args.path}")
        return _os_failed(exc)
    return json.dumps({"path": rel, "entries": entries})


def _read_window(root: Path, user_path: str, offset: int, limit: int) -> tuple[str, int, bytes]:
    """`(rel, size, chunk)`: the file's size, and only the `limit` bytes at `offset` of it.

    The whole file used to be read and then sliced, so `read_file` on a sparse 50 GiB file
    allocated 50 GiB to return the first 512 KB of it. `limit` is capped at `_MAX_READ_BYTES` by
    the argument model and again here, so that is the most one call reads, whatever the size.
    """
    with open_workspace_parent(root, user_path) as (parent, leaf, rel):
        if leaf is None:
            raise NotAFileError(rel)
        fd = open_regular(parent, leaf, os.O_RDONLY, rel)
        try:
            size = os.fstat(fd).st_size
            # Past the end is an empty window, as the slice it replaces was — and an offset past
            # what `off_t` holds never reaches `pread`, which would raise OverflowError on it.
            chunk = _pread(fd, min(limit, _MAX_READ_BYTES), offset) if offset < size else b""
        finally:
            os.close(fd)
    return rel, size, chunk


async def _read_file(args: ReadFileArgs) -> ToolOutput:
    try:
        root = workspace_root()
        # Off the event loop: a bounded read, but a read of a file the agent's code controls,
        # on whatever filesystem the workspace is.
        rel, size, chunk = await asyncio.to_thread(_read_window, root, args.path, args.offset, args.limit)
    except NotAFileError:
        return _refuse(f"not a file: {args.path}")
    except ValueError as exc:
        return _path_refused(exc)
    except OSError as exc:
        if _missing(exc):
            return _refuse(f"not a file: {args.path}")
        return _os_failed(exc)
    try:
        text = chunk.decode("utf-8")
    except UnicodeDecodeError:
        return json.dumps(
            {
                "path": rel,
                "offset": args.offset,
                "binary": True,
                "size": size,
                "bytes_read": len(chunk),
            }
        )
    return json.dumps(
        {
            "path": rel,
            "offset": args.offset,
            "size": size,
            "content": text,
        }
    )


_write_locks: dict[str, asyncio.Lock] = {}


def _write_lock(target: Path) -> asyncio.Lock:
    """One lock per path, so parallel tool calls cannot interleave on a file.

    `spec.tool_execution: parallel` runs a batch with `asyncio.gather`, and two calls in
    one batch can name the same file. Appends would interleave mid-write and a write racing
    an append would drop one of them. The key is the root joined with the normalised relative
    path; with no symlink ever followed, two spellings of one file normalise to one key. The
    map is process-local, which is the same scope as the writes it is ordering.
    """
    return _write_locks.setdefault(str(target), asyncio.Lock())


def _lock_key(root: Path, rel: str) -> Path:
    return root if rel == "." else root.joinpath(*rel.split("/"))


async def _write_file(args: WriteFileArgs) -> ToolOutput:
    try:
        root = workspace_root()
        rel = "/".join(workspace_parts(args.path)) or "."
    except ValueError as exc:
        return _path_refused(exc)
    payload = args.content.encode("utf-8")
    if len(payload) > _MAX_WRITE_BYTES:
        return _refuse(f"content exceeds {_MAX_WRITE_BYTES} bytes")
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if args.append else os.O_TRUNC)
    try:
        async with _write_lock(_lock_key(root, rel)):
            with open_workspace_parent(root, args.path, create=True) as (parent, leaf, rel):
                if leaf is None:
                    return _refuse(f"not a file: {args.path}")
                fd = open_regular(parent, leaf, flags, rel)
                try:
                    _write_all(fd, payload)
                finally:
                    os.close(fd)
    except NotAFileError:
        return _refuse(f"not a file: {args.path}")
    except ValueError as exc:
        return _path_refused(exc)
    except OSError as exc:
        return _os_failed(exc)
    return json.dumps(
        {
            "path": rel,
            "bytes": len(payload),
            "append": args.append,
        }
    )


def _create_edit_temp(parent: int) -> tuple[int, str]:
    """A new, empty sibling in `parent` under a random name: `(fd, name)`."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    for _ in range(3):
        tmp = f"{_EDIT_TMP_PREFIX}{secrets.token_hex(8)}"
        try:
            return open_at(parent, tmp, flags, tmp, 0o600), tmp
        except FileExistsError, SymlinkRefusedError:
            continue  # 64 random bits taken already: draw again, never reuse or delete it
    raise FileExistsError("no free temporary name for the edit")


def _replace_file(parent: int, leaf: str, mode: int, payload: bytes) -> None:
    """Write `payload` over `leaf` in the directory `parent` without ever leaving it half-written.

    A truncating write would leave, on failure partway, a file whose prior contents exist
    nowhere: an edit carries only the two strings, not the pre-image a whole-file write still
    has in its own arguments. The temporary file is a sibling under a random name, made with
    `O_EXCL` through the directory's descriptor (so nothing already there — a planted link, a
    directory — is written through or deleted; a collision just draws another name), and the
    rename — which replaces a name and follows nothing — is atomic. It carries the target's
    mode: an edited `scripts/test.sh` that came back without its executable bit would be a
    strange way to break the gates.
    """
    fd, tmp = _create_edit_temp(parent)
    try:
        try:
            _write_all(fd, payload)
            os.fchmod(fd, mode)
        finally:
            os.close(fd)
        os.replace(tmp, leaf, src_dir_fd=parent, dst_dir_fd=parent)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp, dir_fd=parent)
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
        rel = "/".join(workspace_parts(args.path)) or "."
    except ValueError as exc:
        return _path_refused(exc)
    if len(args.new_string.encode("utf-8")) > _MAX_WRITE_BYTES:
        return _refuse(f"new_string exceeds {_MAX_WRITE_BYTES} bytes")
    try:
        async with _write_lock(_lock_key(root, rel)):
            with open_workspace_parent(root, args.path) as (parent, leaf, rel):
                if leaf is None:
                    return _refuse(f"not a file: {args.path}")
                fd = open_regular(parent, leaf, os.O_RDONLY, rel)
                try:
                    st = os.fstat(fd)
                    # One byte past the cap, never the whole file: a file over it is refused.
                    raw = _pread(fd, _MAX_EDIT_FILE_BYTES + 1, 0)
                    if len(raw) > _MAX_EDIT_FILE_BYTES:
                        return _refuse(f"{args.path} exceeds {_MAX_EDIT_FILE_BYTES} bytes")
                finally:
                    os.close(fd)
                try:
                    text = raw.decode("utf-8")
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
                # Both caps above bound an *input*, and `replace_all` multiplies them: the size
                # of what would be written is projected from the byte delta per match and
                # refused before `str.replace` builds it, because checking afterwards still
                # allocates it.
                grew = len(args.new_string.encode("utf-8")) - len(args.old_string.encode("utf-8"))
                projected = len(raw) + found * grew
                if projected > _MAX_EDIT_FILE_BYTES:
                    return _refuse(
                        f"the edit would make {args.path} {projected} bytes, over the "
                        f"{_MAX_EDIT_FILE_BYTES} limit"
                    )
                # `found` is 1 unless replace_all said otherwise, so this replaces exactly the
                # matches the guards above allowed.
                payload = text.replace(args.old_string, args.new_string).encode("utf-8")
                _replace_file(parent, leaf, stat.S_IMODE(st.st_mode), payload)
    except NotAFileError:
        return _refuse(f"not a file: {args.path}")
    except ValueError as exc:
        return _path_refused(exc)
    except OSError as exc:
        if _missing(exc):
            return _refuse(f"not a file: {args.path}")
        return _os_failed(exc)
    return json.dumps(
        {
            "path": rel,
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


def _scan_text(
    text: str, rel: str, args: SearchFilesArgs, pattern: re.Pattern[str] | None, hits: list[dict[str, Any]]
) -> None:
    for i, line in enumerate(text.splitlines(), start=1):
        # Truncate before matching: backtracking cost grows with the length of the
        # subject, so an unbounded line is what makes a bad pattern expensive.
        subject = line[:_MAX_SEARCH_LINE_CHARS]
        matched = bool(pattern.search(subject)) if pattern else args.query in subject
        if matched:
            hits.append({"path": rel, "line": i, "text": line[:400]})
            if len(hits) >= args.max_hits:
                return


def _scan_file(
    parent: int,
    name: str,
    rel: str,
    args: SearchFilesArgs,
    pattern: re.Pattern[str] | None,
    hits: list[dict[str, Any]],
) -> None:
    try:
        fd = open_regular(parent, name, os.O_RDONLY, rel)
    except ValueError, OSError:
        return  # a symlink, a FIFO, a file that vanished: not searched
    try:
        if os.fstat(fd).st_size > _MAX_SEARCH_FILE_BYTES:
            return
        text = _pread(fd, _MAX_SEARCH_FILE_BYTES, 0).decode("utf-8", errors="ignore")
    except OSError:
        return
    finally:
        os.close(fd)
    _scan_text(text, rel, args, pattern, hits)


def _scan_tree(
    dir_fd: int,
    rel: str,
    args: SearchFilesArgs,
    pattern: re.Pattern[str] | None,
    hits: list[dict[str, Any]],
    deadline: float,
) -> None:
    """Depth-first over `dir_fd`, by descriptor: a symlinked directory is never entered.

    An explicit stack, not recursion, holding one open descriptor per level (closed as each
    level finishes) and `_MAX_SEARCH_DEPTH` levels at most, so a deep tree the agent made can
    neither exhaust the interpreter's stack nor the process's descriptors; deeper directories
    are skipped. Runs on a worker thread that outlives the request's deadline, so it checks the
    deadline itself rather than walking a large tree for nobody, and stops at the hit cap.
    """
    # (descriptor, its rel path, entries still to visit — reverse-sorted, so `pop` is in name
    # order and the walk is the same pre-order the recursive one was). `dir_fd` is the caller's.
    stack = [(dir_fd, rel, sorted(_dir_batch(dir_fd, dirs_and_files_only=True), key=_by_name, reverse=True))]
    try:
        while stack:
            fd, here, pending = stack[-1]
            if not pending:
                stack.pop()
                if fd != dir_fd:
                    os.close(fd)
                continue
            if len(hits) >= args.max_hits or time.monotonic() > deadline:
                return
            name, st = pending.pop()
            child = _child_rel(here, name)
            if stat.S_ISREG(st.st_mode):
                _scan_file(fd, name, child, args, pattern, hits)
                continue
            if len(stack) > _MAX_SEARCH_DEPTH:
                continue
            try:
                sub = open_at(fd, name, _DIR_FLAGS, child)
            except ValueError, OSError:
                continue
            try:
                batch = sorted(_dir_batch(sub, dirs_and_files_only=True), key=_by_name, reverse=True)
            except OSError:
                os.close(sub)
                continue
            stack.append((sub, child, batch))
    finally:
        for fd, _, _ in stack:
            if fd != dir_fd:
                os.close(fd)


def _search(root: Path, args: SearchFilesArgs, pattern: re.Pattern[str] | None) -> list[dict[str, Any]]:
    """Synchronous search, run on a worker thread under a deadline."""
    hits: list[dict[str, Any]] = []
    deadline = time.monotonic() + _SEARCH_BUDGET_S
    with open_workspace_parent(root, args.path) as (parent, leaf, rel):
        if leaf is None:
            _scan_tree(parent, rel, args, pattern, hits, deadline)
            return hits
        mode = os.stat(leaf, dir_fd=parent, follow_symlinks=False).st_mode
        if stat.S_ISLNK(mode):
            raise SymlinkRefusedError(rel)
        if stat.S_ISREG(mode):
            _scan_file(parent, leaf, rel, args, pattern, hits)
            return hits
        fd = open_at(parent, leaf, _DIR_FLAGS, rel)
        try:
            _scan_tree(fd, rel, args, pattern, hits, deadline)
        finally:
            os.close(fd)
    return hits


async def _search_files(args: SearchFilesArgs) -> ToolOutput:
    try:
        root = workspace_root()
        workspace_parts(args.path)
    except ValueError as exc:
        return _path_refused(exc)

    pattern: re.Pattern[str] | None = None
    if args.regex:
        refused = _reject_catastrophic(args.query)
        if refused:
            return _refuse(f"{refused}")
        try:
            pattern = re.compile(args.query)
        except re.error as exc:
            return _refuse(f"invalid regex: {exc}")

    try:
        # Off the event loop and on a deadline. `re` cannot be interrupted, so the thread
        # keeps burning CPU until it finishes — but the request returns and the API stays
        # responsive, which is the difference between a slow tool and a stalled process.
        hits = await asyncio.wait_for(asyncio.to_thread(_search, root, args, pattern), _SEARCH_BUDGET_S)
        return json.dumps({"query": args.query, "hits": hits})
    except TimeoutError:
        return tool_error_output(
            ToolErrorCode.TIMEOUT,
            f"search exceeded {_SEARCH_BUDGET_S:.0f}s — narrow the pattern "
            "(a nested-quantifier regex can be exponential)",
        )
    except ValueError as exc:
        return _path_refused(exc)
    except OSError as exc:
        if _missing(exc):
            return _refuse(f"not found: {args.path}")
        return _os_failed(exc)


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
    "NotAFileError",
    "PathArgs",
    "ReadFileArgs",
    "SearchFilesArgs",
    "SymlinkRefusedError",
    "WriteFileArgs",
    "open_at",
    "open_regular",
    "open_workspace_dir",
    "open_workspace_parent",
    "register_workspace_tools",
    "resolve_under_root",
    "workspace_parts",
    "workspace_root",
]
