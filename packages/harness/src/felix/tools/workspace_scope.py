"""Which directory under FELIX_WORKSPACE_ROOT a workspace tool call works in.

Every workspace tool — and `shell`, the image tools' `path` source and `publish_commits` — reaches
its files through `workspace_root()`. Before scoping that was the deployment's one root for every
tenant and thread. Now the manifest's `spec.workspace.scope` picks a directory under it:

- `thread` (the default): `<root>/.felix-scopes/<tenant>/<hash of tenant and thread>`, so a
  conversation's files are its own.
- `tenant`: `<root>/.felix-scopes/<tenant>/shared`, one workspace every thread of the tenant shares.
- `deployment`: `<root>` itself, for the self-build manifests, whose root is a real checkout.

`deployment` reaches every other scope's files, so it is honoured only for the tenants in
FELIX_WORKSPACE_DEPLOYMENT_TENANTS (by default `default`, the operator's own). Any other tenant
running a manifest that asks for it is refused, never quietly given the root.

The scopes live under one reserved directory rather than at `<root>/<tenant>` so that files a
deployment held before scoping, which sit at the root, can never be mistaken for a tenant's
directory; `felix workspace migrate` moves them into the `default` tenant's `shared` scope.

A thread key is a hash rather than the thread id: a thread id is only guaranteed free of `:` and
`#`, and a hash is a safe path segment whatever the id holds. `tenant_id` is already held to
`[A-Za-z0-9._-]+` and is checked again here before it becomes a path segment.

The scope is bound per call by the builder (`apply_workspace_scope`), outermost, so an approval's
preview reads the same directory the call will write. A call with no scope bound — a tool run
outside any compiled manifest — gets `thread`, the narrowest.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import stat
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from felix.config import Settings

WorkspaceScopeName = Literal["thread", "tenant", "deployment"]

SCOPES_DIR = ".felix-scopes"
SHARED_KEY = "shared"
DEFAULT_SCOPE: WorkspaceScopeName = "thread"

_SCOPE: ContextVar[WorkspaceScopeName | None] = ContextVar("felix_workspace_scope", default=None)


@contextlib.contextmanager
def bound_scope(scope: WorkspaceScopeName) -> Iterator[None]:
    """Run the enclosed tool call against `scope`."""
    token = _SCOPE.set(scope)
    try:
        yield
    finally:
        _SCOPE.reset(token)


def current_scope() -> WorkspaceScopeName:
    return _SCOPE.get() or DEFAULT_SCOPE


def deployment_tenants(settings: Settings) -> frozenset[str]:
    raw = str(getattr(settings, "workspace_deployment_tenants", "") or "")
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def thread_key(tenant_id: str, thread_id: str) -> str:
    return hashlib.sha256(f"{tenant_id}\0{thread_id}".encode()).hexdigest()[:40]


def scope_relpath(
    settings: Settings, tenant_id: str, thread_id: str | None, scope: WorkspaceScopeName
) -> str:
    """The scope's directory relative to the deployment root; `""` is the root itself.

    Raises ValueError, worded "workspace_root: …" so the tools report it as the workspace being
    unavailable rather than as a bad path the model could correct.
    """
    from felix.auth.context import assert_valid_tenant_id

    if scope == "deployment":
        if tenant_id not in deployment_tenants(settings):
            raise ValueError(
                "workspace_root: this agent works in the deployment's whole workspace, which is "
                f"reserved for the operator's tenants (FELIX_WORKSPACE_DEPLOYMENT_TENANTS); "
                f"tenant {tenant_id!r} is not one"
            )
        return ""
    try:
        assert_valid_tenant_id(tenant_id)
    except ValueError as exc:
        raise ValueError(f"workspace_root: tenant id is not a usable directory name: {exc}") from exc
    if scope == "tenant":
        return f"{SCOPES_DIR}/{tenant_id}/{SHARED_KEY}"
    if not thread_id:
        # Refused, not given a shared directory: the manifest asked for a per-thread workspace and
        # a call with no thread has none.
        raise ValueError("workspace_root: this agent's workspace belongs to a thread, and this call has none")
    return f"{SCOPES_DIR}/{tenant_id}/{thread_key(tenant_id, thread_id)}"


def ensure_scope_dir(base: Path, rel: str) -> Path:
    """`base/rel`, created as needed, with no component a symlink. `base` must already be valid.

    Walked by descriptor, so a component swapped for a link between the check and the use is
    refused rather than followed. Created 0700: the API and worker share one uid, and nothing
    else on the host has business in a tenant's files.
    """
    if not rel:
        return base
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(base, flags)
    try:
        for seg in rel.split("/"):
            with contextlib.suppress(FileExistsError):
                os.mkdir(seg, mode=0o700, dir_fd=fd)
            try:
                nxt = os.open(seg, flags, dir_fd=fd)
            except OSError as exc:
                info = os.stat(seg, dir_fd=fd, follow_symlinks=False)
                kind = "a symlink" if stat.S_ISLNK(info.st_mode) else "not a directory"
                raise ValueError(f"workspace_root: {rel} is {kind}") from exc
            os.close(fd)
            fd = nxt
    finally:
        os.close(fd)
    return base.joinpath(*rel.split("/"))


def scoped_root(base: Path, ctx: Any) -> Path:
    """The directory this call's tools work in, for a context `ctx` and the bound scope."""
    tenant_id = str(getattr(getattr(ctx, "auth", None), "tenant_id", "") or "")
    rel = scope_relpath(ctx.settings, tenant_id, getattr(ctx, "thread_id", None), current_scope())
    return ensure_scope_dir(base, rel)


def is_scope_relpath(rel: str) -> bool:
    """Whether `rel` has the shape `scope_relpath` produces: what a shell runner may be sent."""
    if rel == "":
        return True
    parts = rel.split("/")
    if len(parts) != 3 or parts[0] != SCOPES_DIR:
        return False
    from felix.auth.context import assert_valid_tenant_id

    try:
        assert_valid_tenant_id(parts[1])
    except ValueError:
        return False
    key = parts[2]
    return key == SHARED_KEY or (len(key) == 40 and all(c in "0123456789abcdef" for c in key))


@dataclass(frozen=True, slots=True)
class MigrateResult:
    """What `migrate_legacy_files` did, or would do: names relative to the deployment root."""

    target: str
    moved: tuple[str, ...]
    kept: tuple[str, ...]
    # Already present in the target: never overwritten, left at the root for the operator.
    collided: tuple[str, ...]


def migrate_legacy_files(
    base: Path, tenant_id: str = "default", *, keep: frozenset[str] = frozenset(), dry_run: bool = False
) -> MigrateResult:
    """Move what sat at the workspace root before scoping into `tenant_id`'s `shared` scope.

    Before scoping every agent worked at the root, so its files are there; after it, only a
    `deployment`-scope agent sees the root, and every other agent would start empty. This carries
    them to where a `scope: tenant` agent of the default tenant finds them. Every top-level entry
    moves except the scopes directory itself and the names in `keep` (instruction files such as
    AGENTS.md that the operator reads from the root). Nothing is overwritten, so running it again
    is harmless. A rename, so the root and the scopes must be on one filesystem, which they are:
    the scopes live inside the root.
    """
    rel = scope_relpath_for_migration(tenant_id)
    target = base.joinpath(*rel.split("/"))
    moved: list[str] = []
    kept: list[str] = []
    collided: list[str] = []
    for entry in sorted(os.listdir(base)):
        if entry == SCOPES_DIR:
            continue
        if entry in keep:
            kept.append(entry)
            continue
        if os.path.lexists(target / entry):
            collided.append(entry)
            continue
        moved.append(entry)
    if not dry_run and moved:
        ensure_scope_dir(base, rel)
        for entry in moved:
            os.rename(base / entry, target / entry)
    return MigrateResult(target=rel, moved=tuple(moved), kept=tuple(kept), collided=tuple(collided))


def scope_relpath_for_migration(tenant_id: str) -> str:
    from felix.auth.context import assert_valid_tenant_id

    assert_valid_tenant_id(tenant_id)
    return f"{SCOPES_DIR}/{tenant_id}/{SHARED_KEY}"


__all__ = [
    "DEFAULT_SCOPE",
    "SCOPES_DIR",
    "SHARED_KEY",
    "MigrateResult",
    "WorkspaceScopeName",
    "bound_scope",
    "current_scope",
    "deployment_tenants",
    "ensure_scope_dir",
    "is_scope_relpath",
    "migrate_legacy_files",
    "scope_relpath",
    "scoped_root",
    "thread_key",
]
