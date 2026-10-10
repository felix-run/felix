"""The workspace file tools, and the path primitives every workspace consumer walks with.

The five tools (`list_dir`, `read_file`, `write_file`, `edit_file`, `search_files`) judge their
arguments here -- size caps, the regex screen -- and hand the file I/O to a `WorkspaceBackend`
(`felix.tools.workspace_backend`; on this host, `felix.tools.workspace_local`). The primitives
below (`workspace_parts`, `open_workspace_parent`, `open_regular`, ...) are that backend's, and
`shell`, the image tools and the context-file loader's, which work on the local filesystem by
design. `workspace_root()` is the directory those local consumers work in for the current call.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import os
import re
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from felix.context import try_get_context
from felix.tools.errors import ToolErrorCode, tool_error_output
from felix.tools.provider import InMemoryToolProvider
from felix.tools.types import ToolOutput, ToolOutputDict, define_tool
from felix.tools.workspace_backend import PANE_HIDDEN_PREFIX, EditRefused

if TYPE_CHECKING:
    from felix.tools.workspace_backend import WorkspaceBackend, WorkspaceScope

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
# One spelling with the file pane's, which leaves these out of a listing.
_EDIT_TMP_PREFIX = PANE_HIDDEN_PREFIX


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
    of a component is the directory the walk came from. Each component is then a single name:
    no separator, no NUL, nothing `.` or `..`, and at most 255 bytes (NAME_MAX on Linux and
    macOS; past it the open fails with ENAMETOOLONG, which read as an internal error).

    The containment that matters is the walk -- every component opened from its parent's
    descriptor with `O_NOFOLLOW` (`open_workspace_parent`). The normalise-and-prefix check at the
    end restates it in the form a static analyser recognises as a path barrier, and the
    components returned are the ones that passed it.
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
        if os.altsep and os.altsep in seg:
            raise ValueError("path contains a separator other than /")
        if len(seg.encode("utf-8", "surrogateescape")) > 255:
            raise ValueError("path component is longer than 255 bytes")
        parts.append(seg)
    if not parts:
        return []
    anchor = "/workspace-root/"
    checked = os.path.normpath(os.path.join(anchor, *parts))
    if not checked.startswith(anchor):
        raise ValueError("path escapes workspace root")
    return checked[len(anchor) :].split("/")


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
    """The directory every workspace tool — and the shell tool — is confined to.

    A thread with a repository of its own (`felix.repos.checkouts`) works in that checkout and
    nowhere else. Every other run works in its scope's directory under the operator's
    FELIX_WORKSPACE_ROOT (`felix.tools.workspace_scope`): its thread's, its tenant's, or — for the
    operator's own tenants only — the root itself.
    """
    ctx = try_get_context()
    root = ""
    if ctx is not None:
        thread = _thread_checkout(ctx)
        if thread is not None:
            return thread
        root = str(getattr(ctx.settings, "workspace_root", "") or "")
    if not root:
        raise ValueError("workspace_root is not configured (set FELIX_WORKSPACE_ROOT)")
    path = deployment_workspace(root)
    if ctx is None:
        # Not a tool call (no request): operator code, which is given the deployment's root.
        return path
    from felix.tools.workspace_scope import current_scope, scoped_root

    if getattr(ctx.settings, "workspace_backend", "local") == "hosted" and current_scope() != "deployment":
        # The scope's files are in its sandbox, not here: a consumer that needs a real directory
        # (image `path`, `publish_commits`) is refused rather than given one on the host. `shell`
        # does not reach this under `hosted`: it runs in the sandbox (`_hosted_scope` in shell.py).
        from felix.tools.workspace_hosted import local_only_refusal

        raise ValueError(local_only_refusal(current_scope()))
    return scoped_root(path, ctx)


def deployment_workspace(root: str) -> Path:
    """FELIX_WORKSPACE_ROOT, checked: not a link, present, a directory."""
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


def scope_root(settings: Any, scope: WorkspaceScope | None) -> Path:
    """The directory a workspace call for `scope` works in: what `workspace_root()` answers for the
    same request, from explicit arguments rather than the ambient context (the backend's view)."""
    if scope is None:
        return workspace_root()
    thread = _thread_checkout_of(settings, scope.tenant_id, scope.thread_id)
    if thread is not None:
        return thread
    root = str(getattr(settings, "workspace_root", "") or "")
    if not root:
        raise ValueError("workspace_root is not configured (set FELIX_WORKSPACE_ROOT)")
    from felix.tools.workspace_scope import ensure_scope_dir, scope_relpath

    base = deployment_workspace(root)
    return ensure_scope_dir(base, scope_relpath(settings, scope.tenant_id, scope.thread_id, scope.scope))


def _seam() -> tuple[WorkspaceBackend, WorkspaceScope | None]:
    """The backend this call's files are behind, and the scope it is for."""
    from felix.tools.workspace_backend import current_workspace_scope, get_workspace_backend

    settings, scope = current_workspace_scope()
    return get_workspace_backend(settings), scope


def _thread_checkout(ctx: Any) -> Path | None:
    """The run's thread's checkout, or None when the run has no thread or the thread no repo.

    Raises ValueError (worded "workspace_root…") while the checkout exists but cannot be used, so
    a thread whose repository is still cloning or was removed does not fall back to the shared
    workspace and edit files the person never meant it to touch.
    """
    return _thread_checkout_of(
        ctx.settings, getattr(getattr(ctx, "auth", None), "tenant_id", None), getattr(ctx, "thread_id", None)
    )


def _thread_checkout_of(settings: Any, tenant_id: str | None, thread_id: str | None) -> Path | None:
    if not thread_id or not tenant_id:
        return None
    from felix.repos.checkouts import thread_workspace

    try:
        return thread_workspace(settings, tenant_id, thread_id)
    except ValueError as exc:
        message = str(exc)
        raise ValueError(
            message if message.startswith("workspace_root") else f"workspace_root: {message}"
        ) from exc


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
    backend, scope = _seam()
    try:
        listed = await backend.list_dir(scope, args.path)
    except ValueError as exc:
        return _path_refused(exc)
    except OSError as exc:
        if isinstance(exc, FileNotFoundError):
            return _refuse(f"not found: {args.path}")
        if isinstance(exc, NotADirectoryError):
            return _refuse(f"not a directory: {args.path}")
        return _os_failed(exc)
    return json.dumps({"path": listed.path, "entries": listed.entries})


async def _read_file(args: ReadFileArgs) -> ToolOutput:
    backend, scope = _seam()
    try:
        read = await backend.read_file(scope, args.path, args.offset, args.limit)
    except NotAFileError:
        return _refuse(f"not a file: {args.path}")
    except ValueError as exc:
        return _path_refused(exc)
    except OSError as exc:
        if _missing(exc):
            return _refuse(f"not a file: {args.path}")
        return _os_failed(exc)
    try:
        text = read.data.decode("utf-8")
    except UnicodeDecodeError:
        return json.dumps(
            {
                "path": read.path,
                "offset": args.offset,
                "binary": True,
                "size": read.size,
                "bytes_read": len(read.data),
            }
        )
    return json.dumps(
        {
            "path": read.path,
            "offset": args.offset,
            "size": read.size,
            "content": text,
        }
    )


async def _write_file(args: WriteFileArgs) -> ToolOutput:
    backend, scope = _seam()
    try:
        await backend.prepare(scope)
        workspace_parts(args.path)
    except ValueError as exc:
        return _path_refused(exc)
    payload = args.content.encode("utf-8")
    if len(payload) > _MAX_WRITE_BYTES:
        return _refuse(f"content exceeds {_MAX_WRITE_BYTES} bytes")
    try:
        written = await backend.write_file(scope, args.path, payload, args.append)
    except NotAFileError:
        return _refuse(f"not a file: {args.path}")
    except ValueError as exc:
        return _path_refused(exc)
    except OSError as exc:
        return _os_failed(exc)
    return json.dumps(
        {
            "path": written.path,
            "bytes": written.bytes,
            "append": args.append,
        }
    )


async def _edit_file(args: EditFileArgs) -> ToolOutput:
    """Replace an exact string in a file, leaving every other byte where it was
    (`LocalBackend.edit_file` says how)."""
    backend, scope = _seam()
    try:
        await backend.prepare(scope)
        workspace_parts(args.path)
    except ValueError as exc:
        return _path_refused(exc)
    if len(args.new_string.encode("utf-8")) > _MAX_WRITE_BYTES:
        return _refuse(f"new_string exceeds {_MAX_WRITE_BYTES} bytes")
    try:
        edited = await backend.edit_file(scope, args.path, args.old_string, args.new_string, args.replace_all)
    except EditRefused as exc:
        return _refuse(str(exc))
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
            "path": edited.path,
            "replacements": edited.replacements,
            "bytes": edited.bytes,
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


async def _search_files(args: SearchFilesArgs) -> ToolOutput:
    backend, scope = _seam()
    try:
        await backend.prepare(scope)
        workspace_parts(args.path)
    except ValueError as exc:
        return _path_refused(exc)

    if args.regex:
        refused = _reject_catastrophic(args.query)
        if refused:
            return _refuse(f"{refused}")
        try:
            # Compiled here to judge it, before the call reaches a backend; the backend compiles
            # its own copy, since a compiled pattern is not something every backend can be sent.
            re.compile(args.query)
        except re.error as exc:
            return _refuse(f"invalid regex: {exc}")

    try:
        # Off the event loop and on a deadline. `re` cannot be interrupted, so the thread
        # keeps burning CPU until it finishes — but the request returns and the API stays
        # responsive, which is the difference between a slow tool and a stalled process.
        found = await asyncio.wait_for(
            backend.search(scope, args.path, args.query, args.regex, args.max_hits), _SEARCH_BUDGET_S
        )
        return json.dumps({"query": args.query, "hits": found.hits})
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


# The tools `register_workspace_tools` binds, by name: what a manifest's `spec.tools` lists to work
# in the harness's workspace (`felix.usage.catalog.workspace_summary` reads it).
WORKSPACE_TOOL_NAMES = frozenset({"list_dir", "read_file", "write_file", "edit_file", "search_files"})


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
    "WORKSPACE_TOOL_NAMES",
    "EditFileArgs",
    "NotAFileError",
    "PathArgs",
    "ReadFileArgs",
    "SearchFilesArgs",
    "SymlinkRefusedError",
    "WriteFileArgs",
    "deployment_workspace",
    "open_at",
    "open_regular",
    "open_workspace_dir",
    "open_workspace_parent",
    "register_workspace_tools",
    "resolve_under_root",
    "workspace_parts",
    "workspace_root",
]
