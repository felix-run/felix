"""Background shell commands: started by one tool call, read and ended by later ones.

A `shell_tools` command with `background: true` may be started with `run_in_background`. The
start returns an id at once; `shell_output` reads what the command has written since the last
read, and `shell_kill` ends it. Every start still passes the manifest's and the operator's
allowlists, which the caller checks before it gets here: this module adds only *where the command
lives while nobody is waiting for it*.

That place is a supervisor: this file run as a script (`--supervise`), started in its own session
and detached from the process that started it. It execs the argv with the environment it was
given, drains both streams into files with the foreground tool's byte budget, kills the command's
whole process group at its timeout, at the budget, or when `shell_kill` asks, and writes the
outcome last. Every read is of those files, so the API, the shell runner and the hosted sandbox's
helper -- which each run one call and exit, or may restart -- need no memory of their own.

- **The state lives outside the workspace**, under the system temp directory, one directory per
  workspace root (a scope, a thread's checkout), private to this user. The model's file tools never
  reach it. A command run by the same user can, as it can the workspace of any scope it shares a
  host with: where commands run is the boundary (`deploy/GOVERNANCE.md`, "Shell tools").
- **Bounded.** At most `MAX_RUNNING` running per root and `MAX_RUNNING_HOST` on this host, counted
  and started under one lock; at most `MAX_KEPT` kept per root, and a finished (or lost) one is
  forgotten after `KEPT_FOR_S` -- every root's, on every start, so a root nobody returns to is
  swept too. Output is the foreground budget, `MAX_TOTAL_OUTPUT_BYTES`, across both streams, and a
  read returns at most `MAX_OUTPUT_BYTES` of each, skipping what it must.
- **Alive means its supervisor holds a lock**, never a pid that may have been reused: the supervisor
  takes an exclusive `flock` on its entry's `alive` file before it detaches and holds it for its
  life. While it is held the command is running, whatever `status.json` says. The command runs as
  the same user, so it can still replace `alive` or hold a lock on it itself, and hide or pin its
  entry that way: these are bounds on a well-behaved command, not on a hostile one.
- **Ended by the supervisor.** `shell_kill` leaves a marker the supervisor acts on, so nothing is
  signalled from outside.

Standard library only, and copied into the hosted gateway's helper
(`deploy/cloudflare/workspace-gateway/helper/felix_fs.py`), which
`tests/unit/test_workspace_gateway_helper.py` holds to this code.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import secrets
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

# The foreground tool's bounds (`felix.tools.shell`), held equal by a test: a command may write no
# more in the background than it could in the foreground.
MAX_OUTPUT_BYTES = 64_000
MAX_TOTAL_OUTPUT_BYTES = 8_000_000
_READ_CHUNK = 65_536
_MAX_STDIN_CHARS = 256_000
_DRAIN_AFTER_KILL_S = 5.0
MAX_RUNNING = 4
# Across every root one tenant owns on this host (the runner's are shared by every tenant).
MAX_RUNNING_OWNER = 8
MAX_RUNNING_HOST = 32
MAX_KEPT = 16
KEPT_FOR_S = 3600.0
# The longest a read waits for output. Under the hosted gateway's 30s per-operation deadline.
MAX_WAIT_MS = 20_000
# How long `shell_kill` waits for the supervisor to report the command ended.
KILL_WAIT_S = 10.0
# How long a start waits for its supervisor to detach (lock, fork, exit).
DETACH_WAIT_S = 10.0
_POLL_S = 0.2
# The most of a state file read: a command can write its own, and they are read under the lock.
_MAX_STATE_BYTES = 65_536
# How long a start waits for another start's lock, and a read for another read's.
_LOCK_WAIT_S = 10.0
_BACKGROUND_ID = re.compile(r"[0-9a-f]{32}")
_STATES = ("exited", "killed", "timed_out", "output_exceeded", "failed")


class BackgroundLimitReached(Exception):
    """A start refused because enough commands are running already. The message is the model's."""


class UnknownBackgroundId(FileNotFoundError):
    """No background command by that id here. The message is the model's."""


def _supervisor_argv(entry: Path) -> list[str]:
    """How a supervisor is started: this file, isolated from the caller's paths and environment."""
    return [sys.executable, "-I", os.path.abspath(__file__), "--supervise", str(entry)]


def _private_dir(path: Path) -> Path:
    """`path` as a directory only this user can enter, made if missing; refused if it is anything
    else -- a link, another user's, open to others -- since it holds what a command printed."""
    with contextlib.suppress(FileExistsError):
        path.mkdir(mode=0o700)
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise PermissionError(f"refusing background state directory {path}")
    return path


def _state_top() -> Path:
    return _private_dir(Path(tempfile.gettempdir()) / f"felix-shell-bg-{os.getuid()}")


def background_base(root: Path) -> Path:
    """The directory of `root`'s background commands, outside it and private to this user."""
    return _private_dir(_state_top() / hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:32])


@contextlib.contextmanager
def _flocked(path: Path, wait_s: float) -> Any:
    """An exclusive `flock` on `path` for the block, waiting at most `wait_s` for it -- a command
    can take the lock itself -- and raising `TimeoutError` past that."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        deadline = time.monotonic() + wait_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise TimeoutError(f"{path.name} stayed locked") from None
                time.sleep(_POLL_S / 4)
        yield
    finally:
        os.close(fd)


def _read_json(path: Path) -> Any:
    """The file's JSON, or None: missing, not JSON, or larger than any state file is."""
    try:
        with open(path, "rb") as f:
            raw = f.read(_MAX_STATE_BYTES + 1)
        return None if len(raw) > _MAX_STATE_BYTES else json.loads(raw)
    except OSError, ValueError:
        return None


def _write_json(path: Path, value: Any) -> None:
    """Written whole or not at all: a reader sees the old file or the new one."""
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}")
    tmp.write_text(json.dumps(value), encoding="utf-8")
    os.replace(tmp, path)


def _is_entry_dir(path: Path) -> bool:
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def _entry(base: Path, process_id: str) -> Path:
    if not _BACKGROUND_ID.fullmatch(process_id) or not _is_entry_dir(base / process_id):
        raise UnknownBackgroundId(f"no background command {process_id[:64]!r}")
    return base / process_id


def _supervisor_alive(entry: Path) -> bool:
    """Whether the entry's supervisor still holds its `alive` lock."""
    try:
        fd = os.open(entry / "alive", os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return False
    try:
        # Shared: two probes do not hold each other off, and either still meets the supervisor's.
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)
    return False


def _spec(entry: Path) -> dict[str, Any]:
    """What the start recorded, with every field held to its type (a command can write the file)."""
    spec = _read_json(entry / "spec.json")
    spec = spec if isinstance(spec, dict) else {}
    argv = spec.get("argv")
    started = spec.get("started")
    if not isinstance(started, int | float):
        # Not written yet (a start between `mkdir` and its spec), or not a time: as new as the entry.
        try:
            started = os.lstat(entry).st_mtime
        except OSError:
            started = time.time()
    return {
        "argv": argv if isinstance(argv, list) and all(isinstance(a, str) for a in argv) else [],
        "cwd": spec.get("cwd") if isinstance(spec.get("cwd"), str) else "",
        "owner": spec.get("owner") if isinstance(spec.get("owner"), str) else "",
        "started": float(started),
    }


def _status(entry: Path) -> dict[str, Any] | None:
    """The outcome the supervisor wrote, or None for none (or one not in its shape)."""
    status = _read_json(entry / "status.json")
    if not isinstance(status, dict) or status.get("state") not in _STATES:
        return None
    ended, code, error = status.get("ended"), status.get("exit_code"), status.get("error")
    if not isinstance(ended, int | float) or not (code is None or isinstance(code, int)):
        return None
    return {
        "state": status["state"],
        "exit_code": code,
        "error": error if isinstance(error, str) else None,
        "truncated": status.get("truncated") is True,
        "ended": float(ended),
    }


def _state(entry: Path) -> tuple[str, dict[str, Any] | None]:
    """`running` while the supervisor lives; then its outcome, or `lost` with none."""
    if _supervisor_alive(entry):
        return "running", None
    status = _status(entry)
    return (status["state"], status) if status is not None else ("lost", None)


def _describe(entry: Path, root: Path) -> dict[str, Any]:
    spec = _spec(entry)
    state, status = _state(entry)
    ended = status["ended"] if status is not None else time.time()
    return {
        "id": entry.name,
        "argv": spec["argv"],
        "cwd": os.path.relpath(spec["cwd"] or str(root), str(root)),
        "status": state,
        "exit_code": status["exit_code"] if status is not None else None,
        "error": status["error"] if status is not None else None,
        "truncated": status["truncated"] if status is not None else False,
        "duration_ms": max(0, int((ended - spec["started"]) * 1000)),
    }


def _entries(base: Path) -> list[Path]:
    found = [p for p in base.iterdir() if _BACKGROUND_ID.fullmatch(p.name) and _is_entry_dir(p)]
    return sorted(found, key=lambda p: (_spec(p)["started"], p.name))


def _prune(base: Path) -> list[Path]:
    """Forget finished (or lost) commands past `KEPT_FOR_S`, then the oldest finished past
    `MAX_KEPT`. A running one is never forgotten. Returns what is kept, oldest first."""
    now = time.time()
    kept: list[Path] = []
    finished: list[Path] = []
    for entry in _entries(base):
        state, status = _state(entry)
        if state == "running":
            kept.append(entry)
            continue
        # A lost one is as old as when it started.
        ended = status["ended"] if status is not None else _spec(entry)["started"]
        if now - ended > KEPT_FOR_S:
            shutil.rmtree(entry, ignore_errors=True)
            continue
        kept.append(entry)
        finished.append(entry)
    while len(kept) > MAX_KEPT and finished:
        oldest = finished.pop(0)
        shutil.rmtree(oldest, ignore_errors=True)
        kept.remove(oldest)
    return kept


def _running_everywhere(top: Path, base: Path, owner: str) -> tuple[int, int, int]:
    """Prune every root's commands, and count those running here, for `owner` on any root, and
    on the whole host. A root left with nothing is removed, so one nobody returns to does not stay
    behind -- never `base`, which the caller is about to start in."""
    here = owned = everywhere = 0
    for other in top.iterdir():
        if not (_BACKGROUND_ID.fullmatch(other.name) and _is_entry_dir(other)):
            continue
        running = [e for e in _prune(other) if _state(e)[0] == "running"]
        everywhere += len(running)
        if owner:
            owned += sum(1 for e in running if _spec(e)["owner"] == owner)
        if other == base:
            here = len(running)
        elif not any(other.iterdir()):
            with contextlib.suppress(OSError):
                other.rmdir()
    return here, owned, everywhere


def _limit(count: int, cap: int, where: str) -> None:
    if count >= cap:
        raise BackgroundLimitReached(
            f"{cap} background commands are already running {where}; "
            "end one with shell_kill or wait for one to finish"
        )


def start_background(
    argv: list[str],
    *,
    cwd: Path,
    root: Path,
    stdin: str | None,
    timeout_s: float,
    env: dict[str, str],
    owner: str = "",
) -> dict[str, Any]:
    """Start `argv` under a supervisor and return its description, with the id to read it by.
    Raises `BackgroundLimitReached` when `MAX_RUNNING` run here, `MAX_RUNNING_OWNER` for `owner`
    (a tenant; empty counts against no owner's share), or `MAX_RUNNING_HOST` on the host. Counted
    and started under one lock, so concurrent starts cannot all pass the count."""
    top = _state_top()
    spec = {"argv": argv, "cwd": str(cwd), "env": env, "owner": owner, "timeout_s": timeout_s}
    try:
        with _flocked(top / ".lock", _LOCK_WAIT_S):
            entry = _start_locked(top, root, spec, stdin)
    except TimeoutError:
        raise BackgroundLimitReached(
            "background commands are being started here; try again shortly"
        ) from None
    return _describe(entry, root)


def _start_locked(top: Path, root: Path, spec: dict[str, Any], stdin: str | None) -> Path:
    # Under the lock, so another start's sweep cannot remove this root as an empty one.
    base = background_base(root)
    here, owned, everywhere = _running_everywhere(top, base, spec["owner"])
    _limit(here, MAX_RUNNING, "here")
    _limit(owned, MAX_RUNNING_OWNER, "for this tenant")
    _limit(everywhere, MAX_RUNNING_HOST, "on this host")
    entry = base / secrets.token_hex(16)
    entry.mkdir(mode=0o700)
    _write_json(entry / "spec.json", {**spec, "started": time.time()})
    if stdin is not None:
        (entry / "stdin").write_bytes(stdin[:_MAX_STDIN_CHARS].encode("utf-8"))
    # The supervisor locks `alive`, forks and exits, so nothing is left for the caller to reap
    # and the command is running from the moment this returns.
    starter = subprocess.Popen(
        _supervisor_argv(entry),
        env=spec["env"],
        cwd="/",
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        starter.wait(timeout=DETACH_WAIT_S)
    except subprocess.TimeoutExpired:
        # Answered as it stands (`running` or `lost`), never as a failure the model would retry.
        starter.kill()
        starter.wait()
    return entry


def _kill_group_of(proc: subprocess.Popen[bytes]) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)


def _spawn(entry: Path, spec: dict[str, Any]) -> subprocess.Popen[bytes] | None:
    """The command, in its own process group; or None, its failure written as the outcome."""
    stdin_path = entry / "stdin"
    try:
        with open(stdin_path if stdin_path.exists() else os.devnull, "rb") as stdin:
            return subprocess.Popen(
                spec["argv"],
                cwd=spec["cwd"],
                env=spec["env"],
                stdin=stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
    except OSError as exc:
        error = f"{type(exc).__name__}: {exc.strerror or exc}"
        _write_json(
            entry / "status.json",
            {"state": "failed", "exit_code": None, "error": error, "truncated": False, "ended": time.time()},
        )
        return None


def _ending(entry: Path, deadline: float) -> str | None:
    """Why the command must end now -- asked to, or out of time -- or None for no reason yet."""
    if (entry / "kill").exists():
        return "killed"
    if time.monotonic() > deadline:
        return "timed_out"
    return None


def _pump(entry: Path, proc: subprocess.Popen[bytes], deadline: float) -> tuple[str, int]:
    """Drain both streams into files within the byte budget until the command and its group are
    gone, ending it as `_ending` says, or when it exits (its group still goes, so no grandchild
    holds a pipe open). Returns the outcome and the bytes the command wrote."""
    assert proc.stdout is not None and proc.stderr is not None
    reason: str | None = None
    written, drain_until = 0, None
    with (
        open(entry / "out", "wb") as out,
        open(entry / "err", "wb") as err,
        selectors.DefaultSelector() as selector,
    ):
        sinks = {proc.stdout.fileno(): out, proc.stderr.fileno(): err}
        for fd in sinks:
            selector.register(fd, selectors.EVENT_READ)
        while selector.get_map() and (drain_until is None or time.monotonic() < drain_until):
            if drain_until is None:
                reason = reason or _ending(entry, deadline)
                if reason is not None or proc.poll() is not None:
                    _kill_group_of(proc)
                    drain_until = time.monotonic() + _DRAIN_AFTER_KILL_S
            for key, _ in selector.select(timeout=_POLL_S):
                chunk = os.read(key.fd, _READ_CHUNK)
                if not chunk:
                    selector.unregister(key.fd)
                    continue
                room = MAX_TOTAL_OUTPUT_BYTES - written
                if room > 0:
                    sinks[key.fd].write(chunk[:room])
                    sinks[key.fd].flush()
                written += len(chunk)
                if written > MAX_TOTAL_OUTPUT_BYTES and reason is None:
                    reason = "output_exceeded"
    return reason or "exited", written


def _run_supervised(entry: Path) -> None:
    spec = _read_json(entry / "spec.json")
    deadline = time.monotonic() + float(spec["timeout_s"])
    proc = _spawn(entry, spec)
    if proc is None:
        return
    try:
        state, written = _pump(entry, proc, deadline)
    finally:
        _kill_group_of(proc)
        proc.wait()
    status = {
        "state": state,
        "exit_code": proc.returncode,
        "error": None,
        "truncated": written > MAX_TOTAL_OUTPUT_BYTES,
        "ended": time.time(),
    }
    _write_json(entry / "status.json", status)


def supervise(entry: Path) -> int:
    """The supervisor's entry point: lock `alive`, fork, and let this process exit. The child
    inherits the lock and holds it until it exits, outcome written; nothing it starts inherits it."""
    alive = os.open(entry / "alive", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    fcntl.flock(alive, fcntl.LOCK_EX)
    if os.fork():
        return 0
    try:
        _run_supervised(entry)
    finally:
        os._exit(0)


def _read_new(path: Path, offset: int) -> tuple[str, int, int]:
    """What `path` holds past `offset`, at most `MAX_OUTPUT_BYTES` of its end: (text, skipped,
    new offset)."""
    try:
        size = os.stat(path).st_size
    except FileNotFoundError:
        return "", 0, offset
    offset = min(offset, size)
    skipped = max(0, size - offset - MAX_OUTPUT_BYTES)
    with open(path, "rb") as f:
        f.seek(offset + skipped)
        data = f.read(size - offset - skipped)
    return data.decode("utf-8", errors="replace"), skipped, offset + skipped + len(data)


def _cursor(entry: Path) -> dict[str, int]:
    cursor = _read_json(entry / "cursor.json")
    if not isinstance(cursor, dict):
        return {"out": 0, "err": 0}
    return {k: v if isinstance(v := cursor.get(k), int) and v >= 0 else 0 for k in ("out", "err")}


def read_background(root: Path, process_id: str, wait_ms: int) -> dict[str, Any]:
    """`process_id`'s description and what it wrote since the last read, waiting up to `wait_ms`
    (at most `MAX_WAIT_MS`) for something new while it runs. Reads of one command take turns, so
    each byte is handed to one reader; the wait for a turn comes out of the same `wait_ms` (with a
    second's grace), so no read outlasts the hosted gateway's deadline."""
    entry = _entry(background_base(root), process_id)
    deadline = time.monotonic() + min(max(wait_ms, 0), MAX_WAIT_MS) / 1000
    with _flocked(entry / "read.lock", max(deadline - time.monotonic(), 1.0)):
        cursor = _cursor(entry)
        while time.monotonic() < deadline and _supervisor_alive(entry):
            sizes = [os.stat(entry / n).st_size if (entry / n).exists() else 0 for n in ("out", "err")]
            if sizes != [cursor["out"], cursor["err"]]:
                break
            time.sleep(_POLL_S)
        described = _describe(entry, root)
        stdout, out_skipped, cursor["out"] = _read_new(entry / "out", cursor["out"])
        stderr, err_skipped, cursor["err"] = _read_new(entry / "err", cursor["err"])
        _write_json(entry / "cursor.json", cursor)
    return {
        **described,
        "stdout": stdout,
        "stderr": stderr,
        "stdout_skipped": out_skipped,
        "stderr_skipped": err_skipped,
    }


def list_background(root: Path) -> dict[str, Any]:
    """Every background command kept for `root`, oldest first, without output."""
    base = background_base(root)
    return {"processes": [_describe(entry, root) for entry in _prune(base)]}


def kill_background(root: Path, process_id: str) -> dict[str, Any]:
    """Ask `process_id`'s supervisor to end it, and wait a moment for it to say it has."""
    entry = _entry(background_base(root), process_id)
    if _supervisor_alive(entry):
        (entry / "kill").touch()
        deadline = time.monotonic() + KILL_WAIT_S
        while time.monotonic() < deadline and _supervisor_alive(entry):
            time.sleep(_POLL_S)
    return _describe(entry, root)


__all__ = [
    "DETACH_WAIT_S",
    "KEPT_FOR_S",
    "KILL_WAIT_S",
    "MAX_KEPT",
    "MAX_OUTPUT_BYTES",
    "MAX_RUNNING",
    "MAX_RUNNING_HOST",
    "MAX_RUNNING_OWNER",
    "MAX_TOTAL_OUTPUT_BYTES",
    "MAX_WAIT_MS",
    "BackgroundLimitReached",
    "UnknownBackgroundId",
    "background_base",
    "kill_background",
    "list_background",
    "read_background",
    "start_background",
    "supervise",
]


if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "--supervise":
    sys.exit(supervise(Path(sys.argv[2])))
