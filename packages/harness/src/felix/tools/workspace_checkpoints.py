"""File checkpoints: what the model's file tools changed, kept so `/chat/rewind` can undo it.

A rewind moves a thread's leaf back to an earlier event; the conversation forgets what came
after, and before this the workspace did not. Now, while `FELIX_FILE_CHECKPOINTS` is on, each
write, edit, delete or rename the model's workspace tools make in a thread's own workspace
(`tools/workspace.py:_seam`, the one door they share) records the file as it was just before, and
as the tool left it. A rewind with `restore_files` puts back, for every file changed on the branch
it abandons, the state that file had before the branch's first change.

- **Anchored to the conversation.** Each capture names the thread's leaf when it was taken: the
  assistant event whose tool call made the change. The captures a rewind undoes are the ones
  anchored on the branch it abandons (`abandoned_events`) or at the point it leaves from.
- **Refused when the file moved on.** A restore compares the file with what the branch's last
  change left. Anything else -- the person edited it, a shell command rewrote it -- is reported as a
  conflict and left alone.
- **The thread's own workspace only.** A change in a scope threads share (`tenant`, `deployment`)
  is not recorded: undoing one thread's branch there could remove a file another thread relies on.
- **Bounded.** Content is kept once per tenant by its digest, so a file appended to step after
  step is not stored again each time, and the bytes a tenant keeps stop at
  `FELIX_FILE_CHECKPOINT_MAX_BYTES_PER_TENANT`; past it a change is recorded as `unkept`.
- **What it does not see.** A shell command's writes, the operator's file pane, a file over
  `MAX_CHECKPOINT_BYTES` (recorded, not kept). Bookkeeping that fails never blocks the write; it is
  logged and counted (`felix_file_checkpoint_failed`), and that change is not restorable -- if it
  was a file's first change on a branch, a restore puts back the file's next recorded state.

The ledger is `file_checkpoints` with an in-memory twin; content lives in the object store under
`checkpoints/{tenant}/{sha256}`. The retention sweep forgets a thread's captures once its newest is
past `FELIX_FILE_CHECKPOINT_RETENTION_DAYS`, and deletes content no capture names any more.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING, Any, Literal, TypedDict, get_args

from felix.logging_setup import loggable
from felix.observability.metrics import record_counter
from felix.tools import checkpoint_ledger as ledger
from felix.tools.checkpoint_ledger import MAX_RESTORE_CAPTURES, captures, checkpoint_key

if TYPE_CHECKING:
    from felix.config import Settings
    from felix.tools.workspace_backend import WorkspaceBackend, WorkspaceScope

logger = logging.getLogger("felix.tools.workspace_checkpoints")

# The most of a file a checkpoint keeps: the workspace tools' own read cap, so whatever a model
# could have read whole it can have back whole.
MAX_CHECKPOINT_BYTES = 512_000
# The only scope recorded and restored (see the module docstring).
CHECKPOINTED_SCOPE = "thread"

# What a file was before a change: not there, kept (its content is in the store by `before_sha`),
# changed but not kept (the store refused it, or the tenant's ceiling was reached), over the cap, or
# not a regular file.
BeforeState = Literal["absent", "stored", "unkept", "too_large", "unreadable"]
# What the change left: `unknown` until it is recorded, and for good if recording it failed.
AfterState = Literal["absent", "present", "too_large", "unreadable", "unknown"]
Outcome = Literal["restored", "removed", "conflicts", "skipped"]


class RestoreReport(TypedDict):
    restored: list[str]
    removed: list[str]
    conflicts: list[str]
    skipped: list[str]
    # More captures than `MAX_RESTORE_CAPTURES`: files first changed after them were not undone.
    truncated: bool


def empty_report() -> RestoreReport:
    return {"restored": [], "removed": [], "conflicts": [], "skipped": [], "truncated": False}


# --- reading a file's state ---------------------------------------------------------------------


async def file_state(
    backend: WorkspaceBackend, scope: WorkspaceScope | None, path: str
) -> tuple[str, str | None, bytes | None]:
    """`(state, sha256, bytes)`: `present` with its digest and bytes, `absent`, `too_large` (over
    `MAX_CHECKPOINT_BYTES`), or `unreadable` (a directory, a link, an error)."""
    from felix.tools.workspace import NotAFileError

    try:
        read = await backend.read_file(scope, path, 0, MAX_CHECKPOINT_BYTES)
    except FileNotFoundError:
        return "absent", None, None
    except NotAFileError, ValueError, OSError:
        return "unreadable", None, None
    if read.size > len(read.data):
        return "too_large", None, None
    return "present", ledger._sha(read.data), read.data


# --- capturing around the tools' writes ----------------------------------------------------------


class CheckpointingBackend:
    """A workspace backend whose writing operations record each changed file before and after.

    Wrapped around the backend by `_seam` for a call in a thread's run in its own workspace. Every
    member of `WorkspaceBackend` is spelled out: the five a model's file tools write through are
    recorded, the reads pass through, and the folder operations -- which only the operator's pane
    calls, never through `_seam` -- refuse, so a tool that started calling one could not change
    files unrecorded. The capture is fail-open: bookkeeping that fails is logged and counted, and
    the write goes ahead unrecorded."""

    def __init__(
        self, inner: WorkspaceBackend, *, settings: Settings, tenant_id: str, thread_id: str, anchor: str
    ) -> None:
        self._inner = inner
        self._settings = settings
        self._tenant_id = tenant_id
        self._thread_id = thread_id
        self._anchor = anchor

    # -- passed through

    async def prepare(self, scope: WorkspaceScope | None) -> None:
        await self._inner.prepare(scope)

    async def list_dir(self, scope: WorkspaceScope | None, path: str) -> Any:
        return await self._inner.list_dir(scope, path)

    async def read_file(self, scope: WorkspaceScope | None, path: str, offset: int, limit: int) -> Any:
        return await self._inner.read_file(scope, path, offset, limit)

    async def search(
        self, scope: WorkspaceScope | None, path: str, query: str, regex: bool, max_hits: int
    ) -> Any:
        return await self._inner.search(scope, path, query, regex, max_hits)

    async def tree(self, scope: WorkspaceScope | None, limit: int) -> Any:
        return await self._inner.tree(scope, limit)

    # -- not recorded, so not offered

    async def delete_dir(
        self, scope: WorkspaceScope | None, path: str, *, expected_count: int | None = None
    ) -> Any:
        raise RuntimeError("a folder delete is not checkpointed; the file pane calls the backend directly")

    async def rename_dir(self, scope: WorkspaceScope | None, path: str, to_path: str) -> Any:
        raise RuntimeError("a folder rename is not checkpointed; the file pane calls the backend directly")

    # -- recorded

    async def write_file(self, scope: WorkspaceScope | None, path: str, data: bytes, append: bool) -> Any:
        after = None if append else ("present", ledger._sha(data))
        return await self._around(
            scope, [(path, after)], lambda: self._inner.write_file(scope, path, data, append)
        )

    async def edit_file(
        self, scope: WorkspaceScope | None, path: str, old: str, new: str, replace_all: bool
    ) -> Any:
        return await self._around(
            scope, [(path, None)], lambda: self._inner.edit_file(scope, path, old, new, replace_all)
        )

    async def write_file_checked(
        self, scope: WorkspaceScope | None, path: str, data: bytes, expected_sha256: str | None
    ) -> Any:
        return await self._around(
            scope,
            [(path, ("present", ledger._sha(data)))],
            lambda: self._inner.write_file_checked(scope, path, data, expected_sha256),
        )

    async def delete_file(
        self, scope: WorkspaceScope | None, path: str, *, expected_sha256: str | None = None
    ) -> Any:
        return await self._around(
            scope,
            [(path, ("absent", None))],
            lambda: self._inner.delete_file(scope, path, expected_sha256=expected_sha256),
        )

    async def rename_file(
        self, scope: WorkspaceScope | None, path: str, to_path: str, *, expected_sha256: str | None = None
    ) -> Any:
        return await self._around(
            scope,
            [(path, ("absent", None)), (to_path, None)],
            lambda: self._inner.rename_file(scope, path, to_path, expected_sha256=expected_sha256),
        )

    async def _around(
        self,
        scope: WorkspaceScope | None,
        paths: list[tuple[str, tuple[str, str | None] | None]],
        op: Callable[[], Awaitable[Any]],
    ) -> Any:
        """Record each path before `op`, run it, then record what it left: `after` when the
        operation says (a whole write, a delete), else read back. A failed `op` drops its captures:
        nothing changed, so there is nothing to undo."""
        taken: list[tuple[str, dict[str, Any], tuple[str, str | None] | None]] = []
        for path, after in paths:
            rel = _normalised(path)
            if rel is None:
                continue
            row = await self._before(scope, rel)
            if row is not None:
                taken.append((rel, row, after))
        try:
            result = await op()
        except BaseException:
            await self._forget([row for _, row, _ in taken])
            raise
        for rel, row, after in taken:
            await self._after(scope, rel, row["capture_id"], after)
        return result

    async def _before(self, scope: WorkspaceScope | None, rel: str) -> dict[str, Any] | None:
        try:
            state, sha, data = await file_state(self._inner, scope, rel)
            before: str = state
            size, needs_put = 0, False
            if state == "present" and sha is not None and data is not None:
                before, needs_put = await self._keepable(sha, len(data))
                size = len(data) if before == "stored" else 0
            row = {
                "tenant_id": self._tenant_id,
                "thread_id": self._thread_id,
                "capture_id": ledger._capture_id(),
                "anchor_event_id": self._anchor,
                "scope": CHECKPOINTED_SCOPE,
                "path": rel,
                "before_state": before,
                "before_sha": sha,
                "after_state": "unknown",
                "after_sha": None,
                # The content's size on every capture that keeps it: the ceiling counts each digest
                # once, and still counts it after the capture that first kept it is gone.
                "size_bytes": size,
                "created_at": ledger.now_ms(),
            }
            # Row before bytes: a failure between leaves a row whose content is missing, which a
            # restore reports as skipped, never content no row names.
            await ledger._insert(self._settings, row)
        except Exception:
            logger.warning(
                "file checkpoint failed before a write to %s; it is not restorable",
                loggable(rel, limit=300),
                exc_info=True,
            )
            record_counter("felix_file_checkpoint_failed", {"stage": "before"})
            return None
        if needs_put and data is not None:
            await self._keep(row, data)
        return row

    async def _keepable(self, sha: str, size: int) -> tuple[str, bool]:
        """`(before_state, whether the content must be put)`: `stored` and nothing to put when the
        tenant already keeps this content, `stored` and put when it fits the ceiling, else `unkept`.
        Concurrent captures can each pass the check, so the ceiling can be overshot by what is in
        flight -- a few captures of at most `MAX_CHECKPOINT_BYTES` each."""
        from felix.storage import get_object_store

        if await get_object_store(self._settings).exists(checkpoint_key(self._tenant_id, sha)):
            return "stored", False
        ceiling = int(self._settings.file_checkpoint_max_bytes_per_tenant)
        if ceiling and await ledger.kept_bytes(self._settings, self._tenant_id) + size > ceiling:
            record_counter("felix_file_checkpoint_failed", {"stage": "ceiling"})
            return "unkept", False
        return "stored", True

    async def _keep(self, row: dict[str, Any], data: bytes) -> None:
        """The file's content into the object store. Refused, the capture goes on as `unkept`, its
        outcome still recorded, so a restore says the file was changed and could not be put back
        rather than calling it a conflict."""
        from felix.storage import get_object_store

        try:
            await get_object_store(self._settings).put(
                checkpoint_key(self._tenant_id, row["before_sha"]),
                data,
                content_type="application/octet-stream",
            )
            return
        except Exception:
            logger.warning(
                "file checkpoint could not keep %s; it is not restorable",
                loggable(row["path"], limit=300),
                exc_info=True,
            )
            record_counter("felix_file_checkpoint_failed", {"stage": "keep"})
        try:
            await ledger._update(
                self._settings,
                self._tenant_id,
                self._thread_id,
                row["capture_id"],
                before_state="unkept",
                size_bytes=0,
            )
        except Exception:
            logger.warning("marking a file checkpoint unkept failed", exc_info=True)

    async def _after(
        self, scope: WorkspaceScope | None, rel: str, capture_id: str, after: tuple[str, str | None] | None
    ) -> None:
        try:
            if after is None:
                state, sha, _ = await file_state(self._inner, scope, rel)
                after = (state, sha)
            await ledger._update(
                self._settings,
                self._tenant_id,
                self._thread_id,
                capture_id,
                after_state=after[0],
                after_sha=after[1],
            )
        except Exception:
            logger.warning(
                "file checkpoint failed after a write to %s", loggable(rel, limit=300), exc_info=True
            )
            record_counter("felix_file_checkpoint_failed", {"stage": "after"})

    async def _forget(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        try:
            from felix.storage import get_object_store

            await ledger.drop_captures(
                self._settings, get_object_store(self._settings), self._tenant_id, self._thread_id, rows
            )
        except Exception:
            logger.warning("dropping the file checkpoints of a failed write failed", exc_info=True)


def _normalised(path: str) -> str | None:
    from felix.tools.workspace import workspace_parts

    try:
        return "/".join(workspace_parts(path)) or None
    except ValueError:
        return None


def checkpointing(
    backend: WorkspaceBackend, settings: Settings, scope: WorkspaceScope | None
) -> WorkspaceBackend:
    """`backend`, recording file changes when checkpoints are on and the call is part of a
    thread's run in its own workspace, with a leaf to anchor them to; else `backend` itself."""
    if not settings.file_checkpoints or scope is None or not scope.thread_id or not scope.tenant_id:
        return backend
    if scope.scope != CHECKPOINTED_SCOPE:
        return backend
    from felix.session.tree import get_leaf

    anchor = get_leaf(scope.thread_id)
    if not anchor:
        return backend
    return CheckpointingBackend(
        backend, settings=settings, tenant_id=scope.tenant_id, thread_id=scope.thread_id, anchor=anchor
    )


# --- restoring ------------------------------------------------------------------------------------


async def restore_files(
    settings: Settings, tenant_id: str, thread_id: str, anchors: Iterable[str]
) -> RestoreReport:
    """Put back each file changed under `anchors` as it was before the first of those changes.

    Paths are sorted into `restored` (written back), `removed` (it did not exist before), `conflicts`
    (no longer what the last change left, so left alone) and `skipped` (its earlier state was not
    kept). `truncated` says the captures ran past `MAX_RESTORE_CAPTURES`."""
    from felix.storage import get_object_store
    from felix.tools.workspace_backend import WorkspaceChanged, WorkspaceScope, get_workspace_backend

    rows = await captures(settings, tenant_id, thread_id, anchors, limit=MAX_RESTORE_CAPTURES + 1)
    report = empty_report()
    if len(rows) > MAX_RESTORE_CAPTURES:
        rows = rows[:MAX_RESTORE_CAPTURES]
        report["truncated"] = True
    first: dict[str, dict[str, Any]] = {}
    last: dict[str, dict[str, Any]] = {}
    for row in rows:
        first.setdefault(row["path"], row)
        last[row["path"]] = row
    backend = get_workspace_backend(settings)
    store = get_object_store(settings)
    scope = WorkspaceScope(tenant_id, thread_id, CHECKPOINTED_SCOPE)
    for path, origin in first.items():
        try:
            outcome = await _restore_one(backend, store, scope, origin, last[path])
        except WorkspaceChanged:
            outcome = "conflicts"
        except Exception:
            logger.warning(
                "restoring %s from a file checkpoint failed", loggable(path, limit=300), exc_info=True
            )
            outcome = "skipped"
        if outcome is not None:
            report[outcome].append(path)
    for key in get_args(Outcome):
        report[key].sort()
    return report


async def _restore_one(
    backend: WorkspaceBackend,
    store: Any,
    scope: WorkspaceScope,
    origin: dict[str, Any],
    latest: dict[str, Any],
) -> Outcome | None:
    """One file back to `origin`'s before-state, if it is still what `latest` left."""
    from felix.tools.workspace import _reserved, workspace_parts

    path = origin["path"]
    # Every tool refuses these before a capture is taken; refused here too, so no future caller
    # turns a restore into a write into `.git`.
    if origin["scope"] != CHECKPOINTED_SCOPE or _reserved(workspace_parts(path)):
        return "skipped"
    state, sha, _ = await file_state(backend, scope, path)
    left = (latest["after_state"], latest["after_sha"])
    if left[0] not in ("present", "absent") or (state, sha) != left:
        return "conflicts"
    if origin["before_state"] == "absent":
        if state == "absent":
            return None
        await backend.delete_file(scope, path, expected_sha256=sha)
        return "removed"
    if origin["before_state"] != "stored":
        return "skipped"
    key = checkpoint_key(origin["tenant_id"], origin["before_sha"])
    data = await store.get(key)
    if data is None:
        return "skipped"
    if ledger._sha(data) != origin["before_sha"]:
        # Not the content its digest names (a write cut short): useless to every capture naming it,
        # and gone, so the next capture of that content keeps it again instead of trusting `exists`.
        await store.delete(key)
        return "skipped"
    if state == "present" and sha == origin["before_sha"]:
        return None
    await backend.write_file_checked(scope, path, data, sha if state == "present" else None)
    return "restored"


__all__ = [
    "CHECKPOINTED_SCOPE",
    "MAX_CHECKPOINT_BYTES",
    "MAX_RESTORE_CAPTURES",
    "AfterState",
    "BeforeState",
    "CheckpointingBackend",
    "Outcome",
    "RestoreReport",
    "checkpoint_key",
    "checkpointing",
    "empty_report",
    "file_state",
    "restore_files",
]
