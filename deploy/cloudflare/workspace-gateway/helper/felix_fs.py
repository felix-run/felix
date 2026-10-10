#!/usr/bin/env python3
"""felix-fs: the workspace file operations, run inside a scope's sandbox.

The Felix harness's workspace tools reach a hosted workspace through the gateway Worker
(`deploy/cloudflare/workspace-gateway`), whose Durable Object starts this sandbox and runs this
script once per operation. The Sandbox SDK's own file API follows symlinks and has no ranged read, so the
operations a model can drive are done here instead, with the harness's local backend's rules:

- every path is walked from a descriptor of /workspace, one component at a time, with
  `O_NOFOLLOW` -- a symlink anywhere in a path is refused, never followed;
- a read returns at most the asked window of a file, whatever its size;
- an edit is written to a random sibling and renamed over the original;
- a search holds one descriptor per level, stops at a depth, a hit cap and a deadline.

The functions between the PORTED markers are copied from the harness (`felix/tools/workspace.py`,
`felix/tools/workspace_local.py`, `felix/tools/shell.py` and `felix/tools/github_publish.py`) and
must stay the same code:
`tests/unit/test_workspace_gateway_helper.py` compares them with their source as syntax trees, so
a change to either fails until the other matches. The image runs the harness's Python, 3.14, so the
copy is the harness's code as it is.

Standard library only: the sandbox image has Python and nothing else of ours.

Protocol: the Durable Object runs `python3 felix_fs.py` with one JSON request on stdin. The operation
runs and one JSON object is printed: `{"ok": true, "result": {...}}` or `{"ok": false, "error": CODE,
"message": TEXT}`. The exit status is 0 for both; anything else means the helper itself failed.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import errno
import json
import os
import re
import secrets
import signal
import stat
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(os.environ.get("FELIX_FS_ROOT", "/workspace"))

# The harness's limits (felix/tools/workspace.py); the tools check the arguments against them before
# a request is sent, and these hold the line again here.
_MAX_READ_BYTES = 512_000
_MAX_WRITE_BYTES = 512_000
_MAX_EDIT_FILE_BYTES = 4_000_000
_MAX_LIST_ENTRIES = 500
_MAX_SEARCH_HITS = 50
_MAX_SEARCH_FILE_BYTES = 256_000
_MAX_QUERY_CHARS = 512
_MAX_SEARCH_LINE_CHARS = 4_000
_SEARCH_BUDGET_S = 5.0
_MAX_SEARCH_DEPTH = 64
_MAX_DIR_BATCH = 10_000
_EDIT_TMP_PREFIX = ".felix-edit-"
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY
# The shell tool's bounds (felix/tools/shell.py), for `exec`.
MAX_OUTPUT_BYTES = 64_000
MAX_TOTAL_OUTPUT_BYTES = 8_000_000
_READ_CHUNK = 65_536
_MAX_STDIN_CHARS = 256_000
_DRAIN_AFTER_KILL_S = 5.0
# The harness's longest integration timeout (`MAX_INTEGRATION_TIMEOUT_S`).
_MAX_EXEC_TIMEOUT_MS = 3_600_000


@dataclass(frozen=True, slots=True)
class SearchFilesArgs:
    """The fields of the harness's argument model the ported search reads."""

    query: str
    path: str
    max_hits: int


class EditRefused(Exception):
    """An edit the model can correct: no match, several, identical strings, over a size cap."""


# --- PORTED from the harness: do not edit below without editing it there -------------------


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


def _child_rel(rel: str, name: str) -> str:
    return name if rel == "." else f"{rel}/{name}"


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


# --- end of PORTED ---------------------------------------------------------------------------


def _child_env() -> dict[str, str]:
    """A command's environment in the sandbox: a fixed minimal one, not a scrubbed copy.

    On the host the harness scrubs its own environment down to a few keys
    (`felix.security.stdio_policy.stdio_child_env`). Here there is nothing to scrub -- the sandbox
    starts with an empty environment -- so the command gets a PATH of absolute system directories
    and nothing else of anyone's.
    """
    return {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": str(ROOT),
        "LANG": "C.UTF-8",
    }


# --- PORTED from the harness's felix/tools/shell.py: do not edit without editing it there ----


class _Stream:
    """A bounded tail of one output stream, filled by `_drain`."""

    def __init__(self) -> None:
        self.tail = bytearray()
        self.truncated = False


class _Budget:
    """Bytes written across both streams, shared so the kill fires once."""

    def __init__(self) -> None:
        self.total = 0
        self.exceeded = False


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    """SIGKILL the child's whole process group, not only the child.

    `start_new_session=True` made the child a group leader, so grandchildren — the pytest
    a test script spawns — die with it instead of holding the pipes open forever.
    """
    if proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)


async def _drain(
    reader: asyncio.StreamReader | None, into: _Stream, budget: _Budget, proc: asyncio.subprocess.Process
) -> None:
    if reader is None:
        return
    while True:
        chunk = await reader.read(_READ_CHUNK)
        if not chunk:
            return
        budget.total += len(chunk)
        into.tail += chunk
        if len(into.tail) > MAX_OUTPUT_BYTES:
            del into.tail[: len(into.tail) - MAX_OUTPUT_BYTES]
            into.truncated = True
        if budget.total > MAX_TOTAL_OUTPUT_BYTES and not budget.exceeded:
            budget.exceeded = True
            _kill_group(proc)


async def _feed(proc: asyncio.subprocess.Process, data: bytes | None) -> None:
    if proc.stdin is None:
        return
    try:
        if data:
            proc.stdin.write(data)
            await proc.stdin.drain()
    except BrokenPipeError, ConnectionResetError:
        pass  # the command exited without reading; its exit code says so
    finally:
        proc.stdin.close()


def resolve_cwd(root: Path, raw: str) -> Path:
    """`raw` under `root`, or `ValueError` — escapes, absolute paths, symlinks, non-directories.

    Walked the way the workspace tools open a path, so a symlinked component is refused here
    as it is there. The answer is still a name the exec then `chdir`s to, so this is a check,
    not a confinement: where the command runs is the boundary, not where it starts.
    """
    try:
        with open_workspace_dir(root, raw or ".") as (_fd, rel):
            pass
    except OSError:
        raise ValueError(f"not a directory: {raw}") from None
    return root if rel == "." else root.joinpath(*rel.split("/"))


async def exec_argv(
    argv: list[str], *, cwd: Path, root: Path, stdin: str | None, timeout_s: float
) -> dict[str, Any]:
    """Exec `argv` in `cwd` and return the result the model sees. The one exec path.

    Shared by the in-process tool and `felix.shell_runner`, so the scrubbed environment, the
    process-group kill and the bounded tail are the same code on both sides. Callers have
    already checked the allowlist and resolved `cwd` under `root`. Raises `OSError` when the
    command cannot be spawned.
    """
    stdin_bytes = stdin[:_MAX_STDIN_CHARS].encode("utf-8") if stdin is not None else None
    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        env=_child_env(),
        stdin=asyncio.subprocess.PIPE if stdin_bytes is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )

    out, err, budget = _Stream(), _Stream(), _Budget()
    tasks = [
        asyncio.create_task(_feed(proc, stdin_bytes)),
        asyncio.create_task(_drain(proc.stdout, out, budget, proc)),
        asyncio.create_task(_drain(proc.stderr, err, budget, proc)),
    ]
    timed_out = False
    try:
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout_s)
        except TimeoutError:
            timed_out = True
            _kill_group(proc)
            await proc.wait()
        # The group is dead or exited; give its pipes a bounded moment to close.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True), timeout=_DRAIN_AFTER_KILL_S
            )
    finally:
        # Cancellation of the calling task lands here too: nothing it spawned survives it.
        _kill_group(proc)
        for task in tasks:
            task.cancel()
        if proc.returncode is None:
            await proc.wait()
    return {
        "argv": argv,
        "cwd": str(cwd.relative_to(root)),
        "exit_code": proc.returncode,
        "timed_out": timed_out,
        "output_exceeded": budget.exceeded,
        "stdout": out.tail.decode("utf-8", errors="replace"),
        "stderr": err.tail.decode("utf-8", errors="replace"),
        "truncated": out.truncated or err.truncated or budget.exceeded,
        "duration_ms": int((time.monotonic() - started) * 1000),
    }


# --- end of PORTED from shell.py -------------------------------------------------------------


# --- PORTED from the harness's felix/tools/github_publish.py: do not edit without editing it there


_GIT_TIMEOUT_S = 60.0
_GIT_OUTPUT_CAP = 4 * 1024 * 1024
_GIT_PRELUDE = (
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.pager=cat",
    "-c",
    "color.ui=false",
    # `log.showSignature` makes `git log` verify signatures, which runs `gpg.program` — another
    # path the repository's config could name.
    "-c",
    "log.showSignature=false",
)


def _git_env() -> dict[str, str]:
    """No credential and no configuration beyond the repository's own.

    Built from nothing rather than copied: the harness environment holds the GitHub token and
    every other secret the API has, and git hands its environment to anything it spawns.
    """
    path = os.pathsep.join(p for p in os.environ.get("PATH", "").split(os.pathsep) if os.path.isabs(p))
    return {
        "PATH": path or "/usr/local/bin:/usr/bin:/bin",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        # `refs/replace` would let the repository show different content under `head_sha` than
        # the object that sha names; the tree check below would catch it, this prevents it.
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_LITERAL_PATHSPECS": "1",
        "LC_ALL": "C",
    }


@dataclass(slots=True)
class _GitResult:
    out: bytes
    code: int
    err: str
    truncated: bool


async def _git_exec(root: Path, *args: str, stdin: bytes | None, limit: int) -> _GitResult:
    """`_git_run` on the host: git as a subprocess with a fixed prelude and environment."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        *_GIT_PRELUDE,
        *args,
        cwd=str(root),
        env=_git_env(),
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = proc.stdout, proc.stderr
    assert stdout is not None and stderr is not None

    async def _read_out() -> tuple[bytes, bool]:
        buf = bytearray()
        while chunk := await stdout.read(65_536):
            buf += chunk
            if len(buf) > limit:
                # Killed here, not after the gather: a child blocked on a full stdout pipe
                # never closes stderr, so waiting for both first would wait forever.
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                return bytes(buf[:limit]), True
        return bytes(buf), False

    async def _read_err() -> bytes:
        kept = bytearray()
        while chunk := await stderr.read(65_536):
            if len(kept) < 2048:
                kept += chunk
        return bytes(kept[:2048])

    try:
        async with asyncio.timeout(_GIT_TIMEOUT_S):
            if stdin is not None and proc.stdin is not None:
                proc.stdin.write(stdin)
                await proc.stdin.drain()
                proc.stdin.close()
            (out, truncated), err = await asyncio.gather(_read_out(), _read_err())
            code = await proc.wait()
    finally:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
    return _GitResult(out=out, code=code, err=err.decode("utf-8", "replace").strip(), truncated=truncated)


# --- end of PORTED from github_publish.py ----------------------------------------------------


def op_list(req: dict[str, Any]) -> dict[str, Any]:
    with open_workspace_dir(ROOT, req.get("path", ".")) as (fd, rel):
        entries: list[dict[str, Any]] = []
        batch = sorted(_dir_batch(fd), key=lambda e: (e[0].lower(), e[0]))
        for name, st in batch[:_MAX_LIST_ENTRIES]:
            mode = st.st_mode
            kind = "dir" if stat.S_ISDIR(mode) else "symlink" if stat.S_ISLNK(mode) else "file"
            item: dict[str, Any] = {"path": _child_rel(rel, name), "type": kind}
            if stat.S_ISREG(mode):
                item["size"] = st.st_size
            entries.append(item)
    return {"path": rel, "entries": entries}


def op_read(req: dict[str, Any]) -> dict[str, Any]:
    offset = int(req.get("offset", 0))
    limit = int(req.get("limit", _MAX_READ_BYTES))
    if offset < 0 or not 1 <= limit <= _MAX_READ_BYTES:
        raise ValueError("offset must be >= 0 and limit between 1 and the read cap")
    rel, size, chunk = _read_window(ROOT, req["path"], offset, limit)
    return {"path": rel, "size": size, "data": base64.b64encode(chunk).decode("ascii")}


def op_write(req: dict[str, Any]) -> dict[str, Any]:
    payload = base64.b64decode(req["data"], validate=True)
    if len(payload) > _MAX_WRITE_BYTES:
        raise EditRefused(f"content exceeds {_MAX_WRITE_BYTES} bytes")
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if req.get("append") else os.O_TRUNC)
    with open_workspace_parent(ROOT, req["path"], create=True) as (parent, leaf, rel):
        if leaf is None:
            raise NotAFileError(rel)
        fd = open_regular(parent, leaf, flags, rel)
        try:
            _write_all(fd, payload)
        finally:
            os.close(fd)
    return {"path": rel, "bytes": len(payload)}


def op_edit(req: dict[str, Any]) -> dict[str, Any]:
    """`LocalBackend.edit_file`, line for line: the same refusals, in the same order."""
    path, old, new = req["path"], req["old"], req["new"]
    replace_all = bool(req.get("replace_all"))
    with open_workspace_parent(ROOT, path) as (parent, leaf, rel):
        if leaf is None:
            raise NotAFileError(rel)
        fd = open_regular(parent, leaf, os.O_RDONLY, rel)
        try:
            st = os.fstat(fd)
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
        grew = len(new.encode("utf-8")) - len(old.encode("utf-8"))
        projected = len(raw) + found * grew
        if projected > _MAX_EDIT_FILE_BYTES:
            raise EditRefused(
                f"the edit would make {path} {projected} bytes, over the {_MAX_EDIT_FILE_BYTES} limit"
            )
        payload = text.replace(old, new).encode("utf-8")
        _replace_file(parent, leaf, stat.S_IMODE(st.st_mode), payload)
    return {"path": rel, "replacements": found, "bytes": len(payload)}


def op_search(req: dict[str, Any]) -> dict[str, Any]:
    query = str(req["query"])
    if not 1 <= len(query) <= _MAX_QUERY_CHARS:
        raise ValueError("query must be 1 to 512 characters")
    max_hits = max(1, min(int(req.get("max_hits", 20)), _MAX_SEARCH_HITS))
    pattern = re.compile(query) if req.get("regex") else None
    args = SearchFilesArgs(query=query, path=str(req.get("path", ".")), max_hits=max_hits)
    hits = _search(ROOT, args, pattern)
    return {"hits": hits}


def op_exec(req: dict[str, Any]) -> dict[str, Any]:
    """A `shell_tools` command, run here by the shell tool's own exec path.

    The harness has already checked the argv against the manifest's and the operator's allowlists
    and screened it; what this adds is where it runs. `cwd` is resolved under /workspace with no
    symlink followed, as the shell tool resolves it on the host.
    """
    argv = req["argv"]
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        raise KeyError("argv")
    timeout_ms = int(req.get("timeout_ms", 300_000))
    if not 1 <= timeout_ms <= _MAX_EXEC_TIMEOUT_MS:
        raise ValueError(f"timeout_ms must be 1 to {_MAX_EXEC_TIMEOUT_MS}")
    stdin = req.get("stdin")
    cwd = resolve_cwd(ROOT, str(req.get("cwd") or "."))
    return asyncio.run(
        exec_argv(
            argv,
            cwd=cwd,
            root=ROOT,
            stdin=stdin if isinstance(stdin, str) else None,
            timeout_s=timeout_ms / 1000,
        )
    )


def op_git(req: dict[str, Any]) -> dict[str, Any]:
    """A read of the thread's repository for the harness (a listing, `publish_commits`' commits):
    git run by the harness's own `_git_exec`, with its prelude and its environment, in /workspace.
    Unlike `exec`, which keeps a command's *tail* for a model to read, this keeps stdout from the
    start, up to `limit`, as the harness reads it locally."""
    args = req["args"]
    if not isinstance(args, list) or not args or not all(isinstance(a, str) for a in args):
        raise KeyError("args")
    limit = req.get("limit", _GIT_OUTPUT_CAP)
    if not isinstance(limit, int) or not 1 <= limit <= _GIT_OUTPUT_CAP:
        raise KeyError("limit")
    stdin = base64.b64decode(req["stdin"], validate=True) if req.get("stdin") is not None else None
    res = asyncio.run(_git_exec(ROOT, *args, stdin=stdin, limit=limit))
    return {
        "out": base64.b64encode(res.out).decode("ascii"),
        "code": res.code,
        "err": res.err,
        "truncated": res.truncated,
    }


def op_lstat(req: dict[str, Any]) -> dict[str, Any]:
    """`lstat` of each path under /workspace, as a repository listing reports sizes: `null` for one
    that is not there. Paths come from git's own listing; nothing is opened or followed."""
    paths = req["paths"]
    if not isinstance(paths, list) or len(paths) > 10_000 or not all(isinstance(p, str) for p in paths):
        raise KeyError("paths")
    out: list[dict[str, Any] | None] = []
    for path in paths:
        workspace_parts(path)  # absolute or escaping: refused like any other workspace path
        target = ROOT / path
        try:
            st = os.lstat(target)
        except OSError:
            out.append(None)
            continue
        out.append({"kind": "symlink" if stat.S_ISLNK(st.st_mode) else "file", "size": st.st_size})
    return {"stats": out}


def op_prepare(req: dict[str, Any]) -> dict[str, Any]:
    with open_workspace_dir(ROOT, "."):
        pass
    return {}


OPS = {
    "prepare": op_prepare,
    "list": op_list,
    "read": op_read,
    "write": op_write,
    "edit": op_edit,
    "search": op_search,
    "exec": op_exec,
    "git": op_git,
    "lstat": op_lstat,
}


def run(req: dict[str, Any]) -> dict[str, Any]:
    """One request to one JSON answer; every failure the tools map is a code, never a traceback."""
    op = OPS.get(str(req.get("op")))
    if op is None:
        return {"ok": False, "error": "bad_request", "message": f"unknown op {req.get('op')!r}"}
    try:
        return {"ok": True, "result": op(req)}
    except EditRefused as exc:
        return {"ok": False, "error": "edit_refused", "message": str(exc)}
    except NotAFileError as exc:
        return {"ok": False, "error": "not_a_file", "message": str(exc)}
    except re.error as exc:
        return {"ok": False, "error": "bad_request", "message": f"invalid regex: {exc}"}
    except KeyError as exc:
        return {"ok": False, "error": "bad_request", "message": f"missing field {exc}"}
    except ValueError as exc:
        # An escaping or absolute path, a symlink component, a NUL byte: the model's to fix.
        return {"ok": False, "error": "invalid_path", "message": str(exc)}
    except FileNotFoundError as exc:
        return {"ok": False, "error": "not_found", "message": str(exc)}
    except NotADirectoryError as exc:
        return {"ok": False, "error": "not_a_directory", "message": str(exc)}
    except PermissionError as exc:
        return {"ok": False, "error": "permission_denied", "message": str(exc), "kind": type(exc).__name__}
    except OSError as exc:
        # `kind` names the exception, so the harness raises the same one and its tools word the
        # failure exactly as they do for the local backend (`IsADirectoryError: [Errno 21] ...`).
        return {"ok": False, "error": "io_error", "message": str(exc), "kind": type(exc).__name__}


def main(stdin: Any) -> int:
    """Read one request from stdin, answer it on stdout. Exit 0 whatever the answer."""
    try:
        req = json.loads(stdin.read())
    except ValueError:
        req = None
    if not isinstance(req, dict):
        sys.stdout.write(
            json.dumps({"ok": False, "error": "bad_request", "message": "request must be a JSON object"})
        )
        return 0
    sys.stdout.write(json.dumps(run(req)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.stdin.buffer))
