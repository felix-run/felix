"""The `local` workspace backend: the workspace tools' file I/O, on this host, under a scope.

What the five tools did in-process before the seam (`felix.tools.workspace_backend`), moved here
unchanged: every path walked from a descriptor of the scope's directory with no symlink followed
(`open_workspace_parent`), a per-path lock around each write and each edit's read-modify-write, an
edit written to a random sibling and renamed over the original, and a tree search that holds one
descriptor per level, stops at a depth and a hit cap, and checks its own deadline. The primitives
it walks with stay in `felix.tools.workspace`, where `shell`, the image tools and the context-file
loader use them too.

The scope's directory comes from `scope_root` -- the thread's checkout when it has one, otherwise
its scope under FELIX_WORKSPACE_ROOT (`felix.tools.workspace_scope`) -- which is the same answer
`workspace_root()` gives the tools that are not behind this seam.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import hashlib
import os
import re
import secrets
import stat
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from felix.tools.workspace import (
    _DIR_FLAGS,
    _EDIT_TMP_PREFIX,
    _MAX_DIR_BATCH,
    _MAX_EDIT_FILE_BYTES,
    _MAX_LIST_ENTRIES,
    _MAX_READ_BYTES,
    _MAX_SEARCH_DEPTH,
    _MAX_SEARCH_FILE_BYTES,
    _MAX_SEARCH_LINE_CHARS,
    _SEARCH_BUDGET_S,
    NotAFileError,
    SearchFilesArgs,
    SymlinkRefusedError,
    _by_name,
    _child_rel,
    _dir_batch,
    _pread,
    _write_all,
    open_at,
    open_regular,
    open_workspace_dir,
    open_workspace_parent,
    scope_root,
    workspace_parts,
)
from felix.tools.workspace_backend import (
    CheckedWriteResult,
    DeleteResult,
    EditRefused,
    EditResult,
    ListResult,
    ReadResult,
    RenameResult,
    SearchResult,
    TreeResult,
    WorkspaceChanged,
    WorkspaceScope,
    WriteResult,
    pane_hides,
)

if TYPE_CHECKING:
    from felix.config import Settings


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


# The file pane's tree walk: the same budget a search has, since it is the same kind of walk over
# a tree the agent's code may have made as large and as deep as it liked.
_TREE_BUDGET_S = _SEARCH_BUDGET_S


def _pane_key(entry: tuple[str, os.stat_result]) -> tuple[str, str]:
    # `list_dir`'s order: case-folded, then the exact name, which is unique in a directory.
    return (entry[0].lower(), entry[0])


def _tree_batch(fd: int) -> tuple[list[tuple[str, os.stat_result]], bool]:
    """A directory's files and subdirectories for the tree, reverse-sorted for `pop`, and whether
    the directory had more than one batch holds (so the listing of it is incomplete)."""
    batch = _dir_batch(fd, dirs_and_files_only=True)
    kept = sorted((e for e in batch if not pane_hides(e[0])), key=_pane_key, reverse=True)
    return kept, len(batch) >= _MAX_DIR_BATCH


def _tree(root: Path, limit: int) -> TreeResult:
    """Pre-order over the whole scope by descriptor, as `_scan_tree` walks: no symlink is listed or
    entered, one descriptor is held per level, and depth, entries and time are all bounded."""
    entries: list[dict[str, Any]] = []
    truncated = False
    deadline = time.monotonic() + _TREE_BUDGET_S
    with open_workspace_dir(root, ".") as (top, _rel):
        first, full = _tree_batch(top)
        truncated = full
        stack = [(top, ".", first)]
        try:
            while stack:
                fd, here, pending = stack[-1]
                if not pending:
                    stack.pop()
                    if fd != top:
                        os.close(fd)
                    continue
                if len(entries) >= limit or time.monotonic() > deadline:
                    truncated = True
                    break
                name, st = pending.pop()
                child = _child_rel(here, name)
                if stat.S_ISREG(st.st_mode):
                    entries.append({"path": child, "type": "file", "bytes": st.st_size})
                    continue
                entries.append({"path": child, "type": "dir"})
                if len(stack) > _MAX_SEARCH_DEPTH:
                    truncated = True
                    continue
                try:
                    sub = open_at(fd, name, _DIR_FLAGS, child)
                except ValueError, OSError:
                    continue  # swapped for a link since the listing, or gone: not entered
                try:
                    batch, full = _tree_batch(sub)
                except OSError:
                    os.close(sub)
                    continue
                truncated = truncated or full
                stack.append((sub, child, batch))
        finally:
            for fd, _, _ in stack:
                if fd != top:
                    os.close(fd)
    return TreeResult(entries=entries, truncated=truncated)


def _current_state(root: Path, path: str, *, digest: bool) -> tuple[str | None, int | None, int | None]:
    """`(sha256, bytes, mode)` of the regular file at `path`: all None when it is missing.

    The digest is taken only when asked for, and only of a file within the read cap: one larger
    than that was never readable through the file pane, so no caller can hold its hash, and it is
    reported by size alone (`sha256` None, `bytes` set). Raises as the other operations do for a
    path naming a directory, a symlink or something else that is not a regular file.
    """
    try:
        with open_workspace_parent(root, path) as (parent, leaf, rel):
            if leaf is None:
                raise NotAFileError(rel)
            try:
                fd = open_regular(parent, leaf, os.O_RDONLY, rel)
            except FileNotFoundError:
                return None, None, None
            try:
                st = os.fstat(fd)
                mode = stat.S_IMODE(st.st_mode)
                if not digest:
                    return None, st.st_size, mode
                raw = _pread(fd, _MAX_READ_BYTES + 1, 0)
            finally:
                os.close(fd)
    except FileNotFoundError:
        return None, None, None  # a directory on the way is missing: so is the file
    if len(raw) > _MAX_READ_BYTES:
        return None, max(len(raw), st.st_size), mode
    return hashlib.sha256(raw).hexdigest(), len(raw), mode


def _write_checked(root: Path, path: str, data: bytes, expected_sha256: str | None) -> str:
    """Compare, then replace: one synchronous call, so nothing else in this process runs between
    the two once the caller holds the path's lock. Returns the path as the tools report it."""
    current, size, mode = _current_state(root, path, digest=expected_sha256 is not None)
    if expected_sha256 is not None and current != expected_sha256:
        raise WorkspaceChanged(current, size)
    with open_workspace_parent(root, path, create=True) as (parent, leaf, rel):
        if leaf is None:
            raise NotAFileError(rel)
        # A new file gets the mode a tool's write would have given it; an existing one keeps its own.
        _replace_file(parent, leaf, 0o600 if mode is None else mode, data)
    return rel


def _source_state(root: Path, path: str, expected_sha256: str | None) -> tuple[str | None, int]:
    """`(sha256, bytes)` of the regular file a delete or a rename is about to act on.

    Missing is `FileNotFoundError` -- unlike a conditional write, which may create the file, these
    have nothing to act on -- and a digest other than `expected_sha256` is `WorkspaceChanged`.
    """
    current, size, _mode = _current_state(root, path, digest=True)
    if size is None:
        raise FileNotFoundError(errno.ENOENT, "no such file", path)
    if expected_sha256 is not None and current != expected_sha256:
        raise WorkspaceChanged(current, size)
    return current, size


def _still_regular(parent: int, leaf: str, rel: str) -> None:
    """The entry is still a regular file at the moment it is acted on: neither swapped for a
    symlink nor for a directory since it was compared. `lstat`, so nothing is followed."""
    mode = os.stat(leaf, dir_fd=parent, follow_symlinks=False).st_mode
    if stat.S_ISLNK(mode):
        raise SymlinkRefusedError(rel)
    if not stat.S_ISREG(mode):
        raise NotAFileError(rel)


def _delete_checked(root: Path, path: str, expected_sha256: str | None) -> str:
    """Compare, then unlink the one name: one synchronous call, under the path's lock. Returns the
    path as the tools report it."""
    _source_state(root, path, expected_sha256)
    with open_workspace_parent(root, path) as (parent, leaf, rel):
        if leaf is None:
            raise NotAFileError(rel)
        _still_regular(parent, leaf, rel)
        os.unlink(leaf, dir_fd=parent)
    return rel


def _rename_checked(
    root: Path, path: str, to_path: str, expected_sha256: str | None
) -> tuple[str, str, str | None, int]:
    """Compare, then move `path` to `to_path` without replacing anything at the destination.

    Both ends are walked from the root's descriptor with no symlink followed, and the rename is
    one `renameat` between the two directories' descriptors. The destination's missing directories
    are made as a write makes them; a component on the way that is a file is not a place a file can
    go (ValueError). Anything at the destination -- a file, a directory, a link, the source itself --
    refuses the move with `FileExistsError`. Returns `(path, to_path, sha256, bytes)`.
    """
    current, size = _source_state(root, path, expected_sha256)
    with contextlib.ExitStack() as stack:
        src_dir, src_leaf, rel = stack.enter_context(open_workspace_parent(root, path))
        if src_leaf is None:
            raise NotAFileError(rel)
        try:
            dst_dir, dst_leaf, to_rel = stack.enter_context(open_workspace_parent(root, to_path, create=True))
        except NotADirectoryError:
            raise ValueError("the destination's directory is a file") from None
        if dst_leaf is None:
            raise NotAFileError(to_rel)
        _still_regular(src_dir, src_leaf, rel)
        try:
            there = os.stat(dst_leaf, dir_fd=dst_dir, follow_symlinks=False).st_mode
        except FileNotFoundError:
            there = None
        if there is not None and stat.S_ISLNK(there):
            raise SymlinkRefusedError(to_rel)
        if there is not None:
            raise FileExistsError(errno.EEXIST, "the destination exists", to_rel)
        os.rename(src_leaf, dst_leaf, src_dir_fd=src_dir, dst_dir_fd=dst_dir)
    return rel, to_rel, current, size


class LocalBackend:
    """The workspace on this host's filesystem. Stateless: the locks are module-level, so two
    instances order their writes against each other as one would."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _root(self, scope: WorkspaceScope | None) -> Path:
        return scope_root(self._settings, scope)

    async def prepare(self, scope: WorkspaceScope | None) -> None:
        self._root(scope)

    async def list_dir(self, scope: WorkspaceScope | None, path: str) -> ListResult:
        root = self._root(scope)
        with open_workspace_dir(root, path) as (fd, rel):
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
        return ListResult(path=rel, entries=entries)

    async def read_file(self, scope: WorkspaceScope | None, path: str, offset: int, limit: int) -> ReadResult:
        root = self._root(scope)
        # Off the event loop: a bounded read, but a read of a file the agent's code controls,
        # on whatever filesystem the workspace is.
        rel, size, chunk = await asyncio.to_thread(_read_window, root, path, offset, limit)
        return ReadResult(path=rel, size=size, data=chunk)

    async def write_file(
        self, scope: WorkspaceScope | None, path: str, data: bytes, append: bool
    ) -> WriteResult:
        root = self._root(scope)
        rel = "/".join(workspace_parts(path)) or "."
        flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC)
        async with _write_lock(_lock_key(root, rel)):
            with open_workspace_parent(root, path, create=True) as (parent, leaf, rel):
                if leaf is None:
                    raise NotAFileError(rel)
                fd = open_regular(parent, leaf, flags, rel)
                try:
                    _write_all(fd, data)
                finally:
                    os.close(fd)
        return WriteResult(path=rel, bytes=len(data))

    async def edit_file(
        self, scope: WorkspaceScope | None, path: str, old: str, new: str, replace_all: bool
    ) -> EditResult:
        """Replace an exact string in a file, leaving every other byte where it was.

        Bytes in and bytes out: `read_text` would open in universal-newline mode and hand back
        `\n` for every `\r\n`, so writing the result would rewrite every line ending in a CRLF
        file that the edit never touched — and an `old_string` the model copied out of
        `read_file` would not match, because that tool preserves them.

        The lock is held across the read and the write. There is no `await` between them today,
        so within one event loop the body is already atomic and the lock cannot be observed to
        do anything; it is here because an edit is a read-modify-write, and the first person to
        move this I/O to a thread would otherwise have to notice that on their own.
        """
        root = self._root(scope)
        rel = "/".join(workspace_parts(path)) or "."
        async with _write_lock(_lock_key(root, rel)):
            with open_workspace_parent(root, path) as (parent, leaf, rel):
                if leaf is None:
                    raise NotAFileError(rel)
                fd = open_regular(parent, leaf, os.O_RDONLY, rel)
                try:
                    st = os.fstat(fd)
                    # One byte past the cap, never the whole file: a file over it is refused.
                    raw = _pread(fd, _MAX_EDIT_FILE_BYTES + 1, 0)
                    if len(raw) > _MAX_EDIT_FILE_BYTES:
                        raise EditRefused(f"{path} exceeds {_MAX_EDIT_FILE_BYTES} bytes")
                finally:
                    os.close(fd)
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    raise EditRefused(f"not UTF-8 text: {path}") from None
                found = text.count(old)
                if found == 0:
                    raise EditRefused(f"old_string not found in {path}")
                if old == new:
                    raise EditRefused(f"old_string and new_string are identical in {path}")
                if found > 1 and not replace_all:
                    raise EditRefused(
                        f"old_string appears {found} times in {path} — extend it with "
                        "surrounding lines until it is unique, or pass replace_all"
                    )
                # Both caps bound an *input*, and `replace_all` multiplies them: the size of what
                # would be written is projected from the byte delta per match and refused before
                # `str.replace` builds it, because checking afterwards still allocates it.
                grew = len(new.encode("utf-8")) - len(old.encode("utf-8"))
                projected = len(raw) + found * grew
                if projected > _MAX_EDIT_FILE_BYTES:
                    raise EditRefused(
                        f"the edit would make {path} {projected} bytes, over the {_MAX_EDIT_FILE_BYTES} limit"
                    )
                # `found` is 1 unless replace_all said otherwise, so this replaces exactly the
                # matches the guards above allowed.
                payload = text.replace(old, new).encode("utf-8")
                _replace_file(parent, leaf, stat.S_IMODE(st.st_mode), payload)
        return EditResult(path=rel, replacements=found, bytes=len(payload))

    async def tree(self, scope: WorkspaceScope | None, limit: int) -> TreeResult:
        """On a worker thread, under its own deadline, like a search."""
        root = self._root(scope)
        return await asyncio.to_thread(_tree, root, limit)

    async def write_file_checked(
        self, scope: WorkspaceScope | None, path: str, data: bytes, expected_sha256: str | None
    ) -> CheckedWriteResult:
        """The compare and the replace run on a worker thread while this path's lock is held, so a
        tool's `write_file` or `edit_file` in this process cannot land between them.

        The lock is process-local, as the tools' own is. A writer in another process -- a durable
        run on the worker, a `shell` command -- is not ordered against it; the compare narrows that
        window to the microseconds between one read and one rename, and does not close it.
        """
        root = self._root(scope)
        rel = "/".join(workspace_parts(path)) or "."
        async with _write_lock(_lock_key(root, rel)):
            rel = await asyncio.to_thread(_write_checked, root, path, data, expected_sha256)
        return CheckedWriteResult(path=rel, bytes=len(data), sha256=hashlib.sha256(data).hexdigest())

    async def delete_file(
        self, scope: WorkspaceScope | None, path: str, *, expected_sha256: str | None = None
    ) -> DeleteResult:
        """The compare and the unlink on a worker thread under the path's lock, as a checked write."""
        root = self._root(scope)
        rel = "/".join(workspace_parts(path)) or "."
        async with _write_lock(_lock_key(root, rel)):
            rel = await asyncio.to_thread(_delete_checked, root, path, expected_sha256)
        return DeleteResult(path=rel)

    async def rename_file(
        self,
        scope: WorkspaceScope | None,
        path: str,
        to_path: str,
        *,
        expected_sha256: str | None = None,
    ) -> RenameResult:
        """Under both paths' locks, taken in one order whatever the direction of the move, so two
        renames that cross each other cannot each hold one and wait for the other."""
        root = self._root(scope)
        # A set: renaming a file onto itself takes its one lock once (asyncio's lock is not
        # reentrant), and is then refused as a destination that exists.
        targets = {_lock_key(root, "/".join(workspace_parts(p)) or ".") for p in (path, to_path)}
        async with contextlib.AsyncExitStack() as stack:
            for target in sorted(targets, key=str):
                await stack.enter_async_context(_write_lock(target))
            rel, to_rel, sha, size = await asyncio.to_thread(
                _rename_checked, root, path, to_path, expected_sha256
            )
        return RenameResult(path=rel, to_path=to_rel, bytes=size, sha256=sha)

    async def search(
        self, scope: WorkspaceScope | None, path: str, query: str, regex: bool, max_hits: int
    ) -> SearchResult:
        """On a worker thread. `re` cannot be interrupted, so the thread keeps burning CPU until it
        finishes; the caller's deadline returns the request, and the walk checks its own."""
        root = self._root(scope)
        args = SearchFilesArgs(query=query, path=path, regex=regex, max_hits=max_hits)
        pattern = re.compile(query) if regex else None
        return SearchResult(hits=await asyncio.to_thread(_search, root, args, pattern))


__all__ = ["LocalBackend"]
