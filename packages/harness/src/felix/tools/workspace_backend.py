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


class EditRefused(Exception):
    """An edit the model can correct: no match, several, identical strings, over a size cap."""


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
    "EditRefused",
    "EditResult",
    "ListResult",
    "ReadResult",
    "SearchResult",
    "WorkspaceBackend",
    "WorkspaceScope",
    "WriteResult",
    "current_workspace_scope",
    "get_workspace_backend",
]
