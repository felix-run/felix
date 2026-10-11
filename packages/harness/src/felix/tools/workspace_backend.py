"""The seam between the workspace tools and wherever their files live (WORKSPACE.md phase 2b).

The five workspace tools (`list_dir`, `read_file`, `write_file`, `edit_file`, `search_files`)
keep everything a model sees — argument models, size limits, the regex screen, every message and
error code — and hand the file I/O itself to a `WorkspaceBackend`. Today there is one, `local`
(`felix.tools.workspace_local`): the same descriptor walks, locks and atomic rename the tools ran
in-process before. A hosted sandbox backend (phase 3) implements the same five operations
somewhere that holds none of this process's credentials.

What crosses the seam is the scope, not a path on this host: a backend is told which tenant, which
thread and which `spec.workspace.scope` the call is for, and decides itself where that lives.
Failures cross it as the exceptions the tools already map: `ValueError` for a path or a workspace
that cannot be used (worded "workspace_root…" when it is the workspace, not the path),
`NotAFileError`, `OSError` for the filesystem refusing, and `EditRefused` for an edit the model can
correct.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from felix.config import Settings
    from felix.tools.workspace_scope import WorkspaceScopeName


# What the operator's file pane never lists, opens or writes: a repository's internals (a hook
# written there runs the next time git does) and the directory the scopes live in, which only a
# `deployment`-scope root contains and which holds every other scope's files. Plus the temporary
# sibling an edit renames over its target, which exists only for the length of one write.
PANE_HIDDEN_DIRS = frozenset({".git", ".felix-scopes"})
PANE_HIDDEN_PREFIX = ".felix-edit-"


def pane_hides(name: str) -> bool:
    """Whether a path component is one the file pane leaves out (`PANE_HIDDEN_DIRS`)."""
    return name in PANE_HIDDEN_DIRS or name.startswith(PANE_HIDDEN_PREFIX)


@dataclass(frozen=True, slots=True)
class WorkspaceScope:
    """Whose workspace a call is for. The backend maps it to a place; the tools never do."""

    tenant_id: str
    thread_id: str | None
    scope: WorkspaceScopeName


@dataclass(frozen=True, slots=True)
class ListResult:
    path: str
    # `{"path", "type": "file" | "dir" | "symlink", "size"?}`, at most the tool's entry cap.
    entries: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ReadResult:
    path: str
    size: int
    data: bytes


@dataclass(frozen=True, slots=True)
class WriteResult:
    path: str
    bytes: int


@dataclass(frozen=True, slots=True)
class EditResult:
    path: str
    replacements: int
    bytes: int


@dataclass(frozen=True, slots=True)
class SearchResult:
    # `{"path", "line", "text"}`, in walk order, at most `max_hits`.
    hits: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class TreeResult:
    # `{"path", "type": "file" | "dir", "bytes"?}` in pre-order, case-folded name order, at most
    # the caller's limit. Symlinks and anything not a file or a directory are left out: nothing
    # here follows a link, so listing one as a file or a directory would be a lie.
    entries: list[dict[str, Any]] = field(default_factory=list)
    # The walk stopped short: the limit, a directory past the batch, the depth cap, or the time
    # budget. Entries listed are still exact; what is missing is only what comes after them.
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class CheckedWriteResult:
    path: str
    bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class DeleteResult:
    path: str


@dataclass(frozen=True, slots=True)
class RenameResult:
    path: str
    to_path: str
    bytes: int
    # Of the file's bytes, which a rename does not change; None for a file over the read cap, which
    # the pane could never have opened and so has no digest to compare against.
    sha256: str | None


@dataclass(frozen=True, slots=True)
class DeleteFolderResult:
    path: str
    # Regular files removed: what the operator was shown and confirmed (`expected_count`).
    files: int


@dataclass(frozen=True, slots=True)
class RenameFolderResult:
    path: str
    to_path: str


# The most entries -- files, directories, links, anything -- a folder operation walks. A delete
# past it is refused before anything is removed, and so is a rename, which walks the tree to find
# a reserved name inside it. Bounded so one request cannot hold a scope's locks over a tree of any
# size: the pane lists far fewer than this before it says its tree was cut.
_MAX_FOLDER_ENTRIES = 2_000


class ReservedPathError(ValueError):
    """A folder operation's tree holds, or its path names, something the pane never touches
    (`pane_hides`): a repository's `.git`, the scopes directory, an edit's temporary file."""

    def __init__(self, shown: str) -> None:
        super().__init__(f"reserved path: {shown}")


class NotAFolderError(ValueError):
    """A folder operation's path names something other than a directory."""


class FolderTooLarge(Exception):
    """A folder operation refused before acting: more than `_MAX_FOLDER_ENTRIES` entries
    (`count` is how many were seen, which stops one past the cap), or deeper than the walk goes."""

    def __init__(self, count: int, *, deep: bool = False) -> None:
        super().__init__("too_deep" if deep else "too_many_entries")
        self.count = count
        self.deep = deep

    @property
    def detail(self) -> dict[str, Any]:
        return {"detail": "too_deep" if self.deep else "too_many_entries", "count": self.count}


class FolderChanged(Exception):
    """A folder delete found a different number of files than the caller showed the operator.
    `count` is the number there now; nothing was removed."""

    def __init__(self, count: int) -> None:
        super().__init__("workspace_changed")
        self.count = count

    @property
    def detail(self) -> dict[str, Any]:
        return {"detail": "workspace_changed", "count": self.count}


class EditRefused(Exception):
    """An edit the model can correct: no match, several, identical strings, over a size cap."""


class WorkspaceChanged(Exception):
    """A conditional write found the file other than the caller last read it.

    `sha256` is the file's digest now, or None when it is missing -- or, with `bytes` set, when it
    is over the read cap and so was never something the caller could have read and hashed.
    """

    def __init__(self, sha256: str | None, bytes: int | None = None) -> None:
        super().__init__("workspace_changed")
        self.sha256 = sha256
        self.bytes = bytes

    @property
    def detail(self) -> dict[str, Any]:
        """The refusal as a client reads it (`409` on `POST /chat/workspace/write`): a code, a
        digest and a size, and nothing else -- it is built for display, unlike `str()`."""
        return {"detail": "workspace_changed", "sha256": self.sha256, "bytes": self.bytes}


class WorkspaceBackend(Protocol):
    """Every operation takes the call's scope; None means the call came from outside any request,
    which has no workspace, and a backend refuses it as unconfigured ("workspace_root…").
    """

    async def prepare(self, scope: WorkspaceScope | None) -> None:
        """Make sure the scope can be served, before the call's arguments are judged.

        Raises ValueError ("workspace_root…") for a workspace that is not configured or that this
        scope may not use, so that reaches the caller ahead of anything wrong with the arguments.
        """
        ...

    async def list_dir(self, scope: WorkspaceScope | None, path: str) -> ListResult: ...

    async def read_file(
        self, scope: WorkspaceScope | None, path: str, offset: int, limit: int
    ) -> ReadResult: ...

    async def write_file(
        self, scope: WorkspaceScope | None, path: str, data: bytes, append: bool
    ) -> WriteResult: ...

    async def edit_file(
        self, scope: WorkspaceScope | None, path: str, old: str, new: str, replace_all: bool
    ) -> EditResult: ...

    async def search(
        self, scope: WorkspaceScope | None, path: str, query: str, regex: bool, max_hits: int
    ) -> SearchResult: ...

    async def tree(self, scope: WorkspaceScope | None, limit: int) -> TreeResult:
        """Every file and directory under the scope's root, recursively, following no symlink.

        For the operator's file pane (`GET /chat/workspace/tree`), not for a model: skips `.git`
        and the scopes directory (`.felix-scopes`), which the pane must not offer to open.
        """
        ...

    async def write_file_checked(
        self, scope: WorkspaceScope | None, path: str, data: bytes, expected_sha256: str | None
    ) -> CheckedWriteResult:
        """Replace `path` with `data` whole, unless the caller's view of it is stale.

        With `expected_sha256`, the file's current digest is compared under the same per-path
        lock the tools' writes take, and a mismatch -- or a missing file -- raises
        `WorkspaceChanged` without writing. Without it the write is unconditional. Either way the
        file is replaced atomically (written to a sibling and renamed), never truncated in place.
        """
        ...

    async def delete_file(
        self, scope: WorkspaceScope | None, path: str, *, expected_sha256: str | None = None
    ) -> DeleteResult:
        """Remove the regular file at `path`, for the operator's file pane.

        Refuses a directory (`NotAFileError`) and a symlink (ValueError) as every operation does,
        and a missing file with `FileNotFoundError`. With `expected_sha256`, the file's digest is
        compared under the path's lock first and a mismatch raises `WorkspaceChanged`, removing
        nothing.
        """
        ...

    async def rename_file(
        self,
        scope: WorkspaceScope | None,
        path: str,
        to_path: str,
        *,
        expected_sha256: str | None = None,
    ) -> RenameResult:
        """Move the regular file at `path` to `to_path` in the same scope, for the file pane.

        Never replaces anything: a destination that exists (a file, a directory, or `path` itself)
        raises `FileExistsError` and moves nothing. Missing directories on the way to `to_path` are
        made, as a write makes them; one of them being a file is ValueError. The source is refused
        as `delete_file` refuses it, and compared the same way under both paths' locks.
        """
        ...

    async def delete_dir(
        self, scope: WorkspaceScope | None, path: str, *, expected_count: int | None = None
    ) -> DeleteFolderResult:
        """Remove the directory at `path` and everything in it, for the operator's file pane.

        Walks by descriptor and follows nothing: a symlink inside is removed as the link, never its
        target. Refuses the root and a path that is not a directory (`NotAFolderError`), a symlink
        (ValueError), a tree holding a reserved name (`ReservedPathError`) and one over
        `_MAX_FOLDER_ENTRIES` entries (`FolderTooLarge`). With `expected_count`, a tree whose
        regular-file count differs raises `FolderChanged`. Every refusal comes before the first
        removal, and the count and the removal run under the backend's locks for the tree.
        """
        ...

    async def rename_dir(self, scope: WorkspaceScope | None, path: str, to_path: str) -> RenameFolderResult:
        """Move the directory at `path` to `to_path` in the same scope, for the file pane.

        One rename, never replacing anything (`FileExistsError`, `to_path == path` included), never
        into itself or below itself (ValueError). `to_path`'s missing parents are made. Refused as
        `delete_dir` refuses its source, including a tree holding a reserved name or past the cap.
        """
        ...


def current_workspace_scope() -> tuple[Settings, WorkspaceScope | None]:
    """This call's settings and scope, from the request context; scope None outside a request."""
    from felix.config import get_settings
    from felix.context import try_get_context
    from felix.tools.workspace_scope import current_scope

    ctx = try_get_context()
    if ctx is None:
        return get_settings(), None
    tenant_id = str(getattr(getattr(ctx, "auth", None), "tenant_id", "") or "")
    return ctx.settings, WorkspaceScope(tenant_id, getattr(ctx, "thread_id", None), current_scope())


def get_workspace_backend(settings: Settings) -> WorkspaceBackend:
    """The backend FELIX_WORKSPACE_BACKEND names: this host's filesystem, or the hosted gateway."""
    if getattr(settings, "workspace_backend", "local") == "hosted":
        from felix.tools.workspace_hosted import HostedBackend

        return HostedBackend(settings)
    from felix.tools.workspace_local import LocalBackend

    return LocalBackend(settings)


__all__ = [
    "PANE_HIDDEN_DIRS",
    "CheckedWriteResult",
    "DeleteFolderResult",
    "DeleteResult",
    "EditRefused",
    "EditResult",
    "FolderChanged",
    "FolderTooLarge",
    "ListResult",
    "NotAFolderError",
    "ReadResult",
    "RenameFolderResult",
    "RenameResult",
    "ReservedPathError",
    "SearchResult",
    "TreeResult",
    "WorkspaceBackend",
    "WorkspaceChanged",
    "WorkspaceScope",
    "WriteResult",
    "current_workspace_scope",
    "get_workspace_backend",
    "pane_hides",
]
