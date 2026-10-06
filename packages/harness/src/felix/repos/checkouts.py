"""A thread's checkout of a person's repository: where the agent works when a thread has one.

One directory per `(tenant, thread)` under the checkout root, named by a hash of the two, holding
the clone (`repo/`) and its state (`checkout.json`). The filesystem *is* the store: every path
that runs an agent — a chat turn, a resumed durable run, a scheduled job — reaches the workspace
through `workspace_root()`, which is synchronous, and can find a thread's checkout by computing
its directory rather than asking a database. API and worker share the root the way they already
share the workspace volume.

What keeps a person's repository a person's:

- **Outside the shared workspace.** The checkout root is never under FELIX_WORKSPACE_ROOT, which
  every thread without a checkout can read; `checkout_root()` refuses a configuration that nests
  them.
- **The token never touches the checkout.** It reaches `git clone` as environment-only git
  configuration (`GIT_CONFIG_COUNT`/`_KEY_0`/`_VALUE_0`, an `http.extraheader`) in an environment
  built from nothing, so it is in no argv, no `.git/config`, no remote URL, and no environment of
  anything the agent later runs in the checkout.
- **Nothing in the repository runs during the clone.** Hooks off, submodules not followed, the
  `file` and `ext` transports refused; the clone lands in a temporary directory and is renamed
  into place only once it succeeded, so a half-written checkout is never the workspace.
- **Bounded.** A repository over FELIX_REPO_CLONE_MAX_MB is refused before cloning, from GitHub's
  own `size`; a checkout its thread has not used for FELIX_REPO_CHECKOUT_TTL_DAYS is removed by
  the worker (`sweep_expired`), and its state says so rather than the thread silently falling
  back to the shared workspace.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from felix.logging_setup import loggable

if TYPE_CHECKING:
    from felix.config import Settings

logger = logging.getLogger("felix.repos.checkouts")

STATE_FILE = "checkout.json"
USED_FILE = ".used"
LOCK_FILE = ".lock"
REPO_DIR = "repo"

CLONING = "cloning"
READY = "ready"
FAILED = "failed"
EXPIRED = "expired"

# Where repositories are cloned from, and the only URL the credential header is scoped to. One
# constant so the two cannot disagree; the tests point it at a local server.
CLONE_BASE = "https://github.com"

# Clones running in this process, held so the event loop does not drop them mid-flight.
_running: set[asyncio.Task[None]] = set()


class CheckoutRefused(Exception):
    """A checkout that cannot be opened, with a code a client can act on."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def checkout_root(settings: Settings) -> Path:
    """Where threads' checkouts live. Raises ValueError for a root inside the shared workspace."""
    raw = settings.repo_checkout_root.strip() or str(Path(settings.data_dir) / "checkouts")
    root = Path(raw).expanduser().resolve()
    workspace = str(getattr(settings, "workspace_root", "") or "").strip()
    if workspace:
        shared = Path(workspace).expanduser().resolve()
        if root == shared or shared in root.parents:
            raise ValueError(
                "FELIX_REPO_CHECKOUT_ROOT is inside FELIX_WORKSPACE_ROOT, where every thread could "
                "read every other thread's repository; put it elsewhere"
            )
    return root


def _key(tenant_id: str, thread_id: str) -> str:
    return hashlib.sha256(f"{tenant_id}\0{thread_id}".encode()).hexdigest()[:40]


def thread_dir(settings: Settings, tenant_id: str, thread_id: str) -> Path:
    return checkout_root(settings) / _key(tenant_id, thread_id)


def read_checkout(settings: Settings, tenant_id: str, thread_id: str) -> dict[str, Any] | None:
    """The thread's checkout state, or None when it has none."""
    return _read_state(thread_dir(settings, tenant_id, thread_id))


def _read_state(directory: Path) -> dict[str, Any] | None:
    try:
        data = json.loads((directory / STATE_FILE).read_text("utf-8"))
    except FileNotFoundError:
        return None
    except OSError, ValueError:
        logger.warning("unreadable checkout state in %s", directory)
        return {"state": FAILED, "error": "the checkout's state could not be read"}
    return data if isinstance(data, dict) else None


def _write_state(directory: Path, data: dict[str, Any]) -> None:
    """Atomically: a reader sees the old state or the new one, never half of either."""
    tmp = directory / f".{STATE_FILE}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(data, sort_keys=True), "utf-8")
    os.replace(tmp, directory / STATE_FILE)


def thread_workspace(settings: Settings, tenant_id: str, thread_id: str) -> Path | None:
    """For `workspace_root()`: the thread's checkout, or None when the thread has none.

    Raises ValueError (worded with "workspace_root", which the workspace tools report as the
    workspace being unavailable rather than as the model's mistake) while the checkout is not
    usable: still cloning, failed, or expired. A thread that had a repository does not quietly
    go back to the shared workspace when it is gone.
    """
    directory = thread_dir(settings, tenant_id, thread_id)
    state = _read_state(directory)
    if state is None:
        return None
    status = state.get("state")
    repo = state.get("repo", "the repository")
    if status == READY:
        path = directory / REPO_DIR
        if path.is_symlink() or not path.is_dir():
            raise ValueError(f"workspace_root: this thread's checkout of {repo} is missing")
        with contextlib.suppress(OSError):
            (directory / USED_FILE).touch()
        return path.resolve()
    if status == CLONING:
        raise ValueError(f"workspace_root: this thread's checkout of {repo} is still cloning")
    if status == EXPIRED:
        raise ValueError(
            f"workspace_root: this thread's checkout of {repo} was removed after "
            f"{settings.repo_checkout_ttl_days} days unused; open the repository again"
        )
    raise ValueError(f"workspace_root: this thread's checkout of {repo} failed: {state.get('error', '')}")


def _auth_env(token: str) -> dict[str, str]:
    """The token as environment-only git configuration, for one git command and its children.

    `http.<url>.extraheader` scoped to github.com: git sends it to GitHub and nowhere else, and
    since it is not in any config file it is gone when the command exits.
    """
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"http.{CLONE_BASE}/.extraheader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
    }


def _scrub(text: str, token: str) -> str:
    """git's own error, minus anything that could carry the credential."""
    out = text.replace(token, "[redacted]")
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return out.replace(basic, "[redacted]")


async def open_checkout(
    settings: Settings,
    tenant_id: str,
    thread_id: str,
    *,
    repo: dict[str, Any],
    github_user_id: int,
    opened_by: str,
    token: str,
    base: str | None = None,
) -> dict[str, Any]:
    """Start cloning `repo` (GitHub's repository object) into the thread's checkout.

    Returns the state at once — `cloning` — and finishes in the background. A thread holds one
    repository: opening the one it already has is a no-op, opening another is refused until the
    first is removed. Raises CheckoutRefused.
    """
    full_name = str(repo.get("full_name") or "")
    branch = base or str(repo.get("default_branch") or "")
    if not full_name or "/" not in full_name or not branch:
        raise CheckoutRefused("invalid_repository", "GitHub returned no repository name or branch")
    size_kb = int(repo.get("size") or 0)
    if size_kb > settings.repo_clone_max_mb * 1024:
        raise CheckoutRefused(
            "repository_too_large",
            f"{full_name} is {size_kb // 1024} MB; checkouts here are capped at "
            f"{settings.repo_clone_max_mb} MB",
        )
    directory = thread_dir(settings, tenant_id, thread_id)
    current = _read_state(directory)
    if current is not None and current.get("state") in {CLONING, READY}:
        if current.get("repo") == full_name:
            return current
        raise CheckoutRefused(
            "thread_has_repository", f"this thread already has {current.get('repo')}; remove it first"
        )
    directory.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(directory / LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise CheckoutRefused("checkout_busy", "a checkout of this thread is already being made") from exc
    os.close(fd)
    state = {
        "state": CLONING,
        "repo": full_name,
        "base": branch,
        "private": bool(repo.get("private")),
        "github_user_id": github_user_id,
        "opened_by": opened_by,
        "size_kb": size_kb,
        "created_at": int(time.time() * 1000),
    }
    try:
        _write_state(directory, state)
        task = asyncio.create_task(_clone(settings, directory, state, token))
    except BaseException:
        with contextlib.suppress(OSError):
            (directory / LOCK_FILE).unlink()
        raise
    _running.add(task)
    task.add_done_callback(_running.discard)
    return state


async def wait_for_clones() -> None:
    """Every clone this process started, finished. For tests and an orderly shutdown."""
    while _running:
        await asyncio.gather(*list(_running), return_exceptions=True)


async def _clone(settings: Settings, directory: Path, state: dict[str, Any], token: str) -> None:
    from felix.tools.github_publish import _GIT_PRELUDE, _git_env

    tmp = directory / f".clone-{os.getpid()}"
    shutil.rmtree(tmp, ignore_errors=True)
    final = {**state}
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *_GIT_PRELUDE,
            "-c",
            "protocol.file.allow=never",
            "-c",
            "protocol.ext.allow=never",
            "-c",
            "submodule.recurse=false",
            "clone",
            "--no-recurse-submodules",
            "--branch",
            state["base"],
            "--origin",
            "origin",
            "--",
            f"{CLONE_BASE}/{state['repo']}.git",
            str(tmp),
            cwd=str(directory),
            env={**_git_env(), **_auth_env(token)},
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            async with asyncio.timeout(settings.repo_clone_timeout_seconds):
                _, err = await proc.communicate()
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
            raise RuntimeError(f"clone timed out after {settings.repo_clone_timeout_seconds:.0f}s") from None
        if proc.returncode != 0:
            raise RuntimeError(
                f"git clone failed ({proc.returncode}): {_scrub(err.decode('utf-8', 'replace'), token)}"
            )
        shutil.rmtree(directory / REPO_DIR, ignore_errors=True)
        os.replace(tmp, directory / REPO_DIR)
        final.update(state=READY, ready_at=int(time.time() * 1000))
        (directory / USED_FILE).touch()
        logger.info(
            "checkout ready: %s for %s",
            loggable(state["repo"], limit=200),
            loggable(state["opened_by"], limit=80),
        )
    except Exception as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        detail = _scrub(str(exc), token)
        # The state is what a client reads back, so it says what went wrong in words chosen here;
        # git's own text names this server's paths and goes to the log only.
        # Classified on git's whole message: its first line names the clone's path, and a long
        # checkout root once pushed the part that said why past a cut made before this.
        final.update(state=FAILED, error=_failure_reason(detail, settings))
        logger.warning(
            "checkout failed: %s: %s", loggable(state["repo"], limit=200), loggable(detail[-600:], limit=600)
        )
    finally:
        _write_state(directory, final)
        with contextlib.suppress(OSError):
            (directory / LOCK_FILE).unlink()


def _failure_reason(detail: str, settings: Settings) -> str:
    """What a failed clone tells a client, classified from git's (scrubbed) error."""
    lowered = detail.lower()
    if "timed out" in lowered:
        return f"the clone took longer than {settings.repo_clone_timeout_seconds:.0f}s"
    if "remote branch" in lowered and "not found" in lowered:
        return "that branch does not exist"
    if "not found" in lowered or "authentication failed" in lowered or "could not read" in lowered:
        return "GitHub refused the clone for this account"
    if "no space left" in lowered:
        return "this server ran out of disk space during the clone"
    return "git could not clone the repository; the cause is in the server's log"


async def describe(settings: Settings, tenant_id: str, thread_id: str) -> dict[str, Any] | None:
    """The checkout's state, and for a ready one what git says: branch, commits ahead, dirty."""
    from felix.tools.github_publish import _git_run

    directory = thread_dir(settings, tenant_id, thread_id)
    state = _read_state(directory)
    if state is None:
        return None
    out: dict[str, Any] = {
        k: state.get(k) for k in ("state", "repo", "base", "private", "opened_by", "created_at", "error")
    }
    if state.get("state") == READY and (directory / REPO_DIR).is_dir():
        root = directory / REPO_DIR
        branch = await _git_run(root, "rev-parse", "--abbrev-ref", "HEAD")
        ahead = await _git_run(root, "rev-list", "--count", f"origin/{state['base']}..HEAD")
        dirty = await _git_run(root, "status", "--porcelain", "--untracked-files=normal")
        out["branch"] = branch.out.decode().strip() if branch.code == 0 else None
        out["ahead"] = int(ahead.out.decode().strip() or 0) if ahead.code == 0 else None
        out["dirty"] = bool(dirty.out.strip()) if dirty.code == 0 else None
    return out


# A listing's default and ceiling. The workspace's tree windows at 200 rows, so a few thousand
# paths is more than a page will ever draw; the ceiling bounds what one request can make git do.
LIST_DEFAULT = 2_000
LIST_MAX = 10_000


class ListRefused(Exception):
    """A listing that cannot be made, with a code a client can act on."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _clean_prefix(prefix: str) -> str:
    """`prefix` as a relative directory inside the checkout, or ListRefused. Lexical only: the
    listing never touches the filesystem through it."""
    raw = prefix.strip("/")
    if not raw:
        return ""
    parts = raw.split("/")
    if "\\" in raw or "\0" in raw or any(p in {"", ".", ".."} for p in parts):
        raise ListRefused("invalid_prefix", "prefix must be a relative path inside the repository")
    return raw + "/"


_STATUS = {"M": "modified", "T": "modified", "A": "added", "D": "deleted", "U": "conflicted"}


def _status_of(xy: str) -> str:
    """git's two-column porcelain code as one word, the worktree column first: what the agent did
    since the last commit is what a reader wants to see."""
    if xy == "??":
        return "untracked"
    for column in (xy[1:2], xy[0:1]):
        if column in _STATUS:
            return _STATUS[column]
    return "modified"


async def list_files(
    settings: Settings, tenant_id: str, thread_id: str, *, prefix: str = "", limit: int = LIST_DEFAULT
) -> dict[str, Any] | None:
    """The thread's checkout's files: tracked ones and untracked ones git does not ignore, each
    with its size and git status, relative to the checkout root and sorted by path.

    Read with the publish tool's environment and prelude: no hooks, no repository-supplied config.
    Sizes come from `lstat`, so a symlink is measured and listed as a link and never followed —
    a link pointing out of the checkout says where it points to no one. None when the thread has
    no checkout; ListRefused while it is cloning or for a bad prefix. A failed or expired checkout
    answers its state with no files.
    """
    from felix.tools.github_publish import _git_run

    directory = thread_dir(settings, tenant_id, thread_id)
    state = _read_state(directory)
    if state is None:
        return None
    status = state.get("state")
    if status == CLONING:
        raise ListRefused("checkout_cloning", "the repository is still cloning")
    if status != READY:
        return {"state": status, "files": [], "truncated": False}
    root = directory / REPO_DIR
    if root.is_symlink() or not root.is_dir():
        return {"state": FAILED, "files": [], "truncated": False}
    want = _clean_prefix(prefix)
    limit = max(1, min(limit, LIST_MAX))
    pathspec = ["--", want] if want else []

    # Literal pathspecs: a prefix is a directory name, never a glob or `:(magic)`.
    listed = await _git_run(
        root, "--literal-pathspecs", "ls-files", "-z", "--cached", "--others", "--exclude-standard", *pathspec
    )
    changed = await _git_run(
        root,
        "--literal-pathspecs",
        "status",
        "--porcelain=v1",
        "-z",
        "--no-renames",
        "--untracked-files=all",
        *pathspec,
    )
    # Output past the cap kills git, so a truncated run's exit code is not a failure.
    if (listed.code != 0 and not listed.truncated) or (changed.code != 0 and not changed.truncated):
        logger.warning("listing checkout failed: %s", loggable(listed.err or changed.err, limit=300))
        raise ListRefused("listing_failed", "the repository could not be listed")

    statuses: dict[str, str] = {}
    entries = changed.out.split(b"\0")
    if changed.truncated:
        entries = entries[:-1]
    for entry in entries:
        if len(entry) > 3:
            statuses[entry[3:].decode("utf-8", "surrogateescape")] = _status_of(
                entry[:2].decode("ascii", "replace")
            )

    # `ls-files` repeats a path once per conflict stage; a set keeps one row each. Paths a status
    # names that `ls-files` cannot (a deletion staged in the index) are listed too.
    names = listed.out.split(b"\0")
    if listed.truncated:
        names = names[:-1]  # the last name was cut mid-path
    paths = {p.decode("utf-8", "surrogateescape") for p in names if p}
    paths |= set(statuses)
    ordered = sorted(paths)
    truncated = len(ordered) > limit or listed.truncated or changed.truncated
    files: list[dict[str, Any]] = []
    for path in ordered[:limit]:
        target = root / path
        try:
            st = os.lstat(target)
        except OSError:
            files.append(
                {"path": path, "kind": "missing", "size": None, "status": statuses.get(path, "deleted")}
            )
            continue
        kind = "symlink" if os.path.islink(target) else "file"
        files.append({"path": path, "kind": kind, "size": st.st_size, "status": statuses.get(path, "clean")})
    return {"state": READY, "files": files, "truncated": truncated}


def remove_checkout(settings: Settings, tenant_id: str, thread_id: str) -> bool:
    """Delete the thread's checkout and its state. True when there was one."""
    directory = thread_dir(settings, tenant_id, thread_id)
    if not (directory / STATE_FILE).exists():
        return False
    if (directory / LOCK_FILE).exists():
        raise CheckoutRefused("checkout_busy", "the repository is still cloning; remove it once it finishes")
    shutil.rmtree(directory)
    return True


def sweep_expired(settings: Settings, *, now: float | None = None) -> int:
    """Remove every checkout unused for FELIX_REPO_CHECKOUT_TTL_DAYS, keeping its state as
    `expired` so the thread says why its repository is gone. Returns how many were removed."""
    root = checkout_root(settings)
    if not root.is_dir():
        return 0
    cutoff = (now if now is not None else time.time()) - settings.repo_checkout_ttl_days * 86_400
    removed = 0
    for directory in root.iterdir():
        if not directory.is_dir() or directory.is_symlink():
            continue
        state = _read_state(directory)
        if state is None or state.get("state") not in {READY, FAILED}:
            continue
        if (directory / LOCK_FILE).exists():
            continue
        try:
            used = (directory / USED_FILE).stat().st_mtime
        except FileNotFoundError:
            used = (directory / STATE_FILE).stat().st_mtime
        if used >= cutoff:
            continue
        shutil.rmtree(directory / REPO_DIR, ignore_errors=True)
        _write_state(directory, {**state, "state": EXPIRED, "expired_at": int(time.time() * 1000)})
        removed += 1
    if removed:
        logger.info("removed %d unused checkout(s)", removed)
    return removed
