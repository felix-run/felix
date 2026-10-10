"""Which workspace a thread's files are in, for the operator's file pane.

The workspace tools find their directory from the run that calls them: the manifest's
`spec.workspace.scope`, bound per call by the builder, and the thread's repository checkout when
it has one (`felix.tools.workspace.scope_root`). An HTTP request from the file pane has no run, so
this answers the same question from what the harness has recorded about the thread -- and the pane
reads from and writes back to exactly the place the agent's next turn will look.

The manifest decides the scope; the caller never does. Which manifest:

1. the one the thread's newest turn ran under (`thread_state.LAST_MANIFEST_KEY`, falling back to
   the pin's `manifest_name` for a thread recorded before that key existed);
2. for a thread that has never run, the manifest the caller names -- the agent its first message
   will go to -- resolved for the caller's tenant like any other;
3. otherwise the deployment's default (`FELIX_DEFAULT_MANIFEST`).

A recorded or default manifest that no longer resolves leaves the thread on `thread` scope, the
narrowest; a manifest the caller named that does not resolve is refused (`UnknownManifest`), since
listing some other workspace in its place would answer a question nobody asked.

A thread with a repository checkout works in the checkout whatever its manifest's scope, exactly as
the tools do; `root_kind` says which of the two the pane is looking at.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from felix.tools.workspace_backend import WorkspaceScope

if TYPE_CHECKING:
    from felix.config import Settings
    from felix.tools.workspace_scope import WorkspaceScopeName

logger = logging.getLogger("felix.workspace_files")

RootKind = Literal["scoped", "checkout"]

# The pane's tree: what a listing returns when not asked, and the most it ever returns.
TREE_DEFAULT_LIMIT = 2_000
TREE_MAX_LIMIT = 5_000


class UnknownManifest(LookupError):
    """The caller named a manifest that does not resolve for its tenant."""


@dataclass(frozen=True, slots=True)
class ThreadWorkspace:
    """Where a thread's workspace is, and why."""

    scope: WorkspaceScope
    # The manifest whose `spec.workspace.scope` decided it, or None when none resolved.
    manifest: str | None
    root_kind: RootKind

    @property
    def scope_name(self) -> WorkspaceScopeName | None:
        """The manifest's scope, or None when a checkout overrides it."""
        return None if self.root_kind == "checkout" else self.scope.scope


async def resolve_thread_workspace(
    settings: Settings, tenant_id: str, thread_id: str, requested_manifest: str | None = None
) -> ThreadWorkspace:
    """The workspace `thread_id` (already tenant-scoped) works in. See the module docstring.

    Raises `UnknownManifest` for a `requested_manifest` that does not resolve, and ValueError
    (worded "workspace_root…") for a checkout that exists but cannot be used -- still cloning,
    failed or expired -- as the tools do, rather than quietly showing the scoped workspace.
    """
    from felix.session.thread_state import LAST_MANIFEST_KEY, get_thread_meta

    meta = await get_thread_meta(settings=settings, tenant_id=tenant_id, thread_id=thread_id)
    recorded = str(meta.get(LAST_MANIFEST_KEY) or meta.get("manifest_name") or "") or None
    name = recorded or requested_manifest or settings.default_manifest
    scope_name = await _manifest_scope(
        settings, tenant_id, thread_id, name, strict=recorded is None and requested_manifest is not None
    )
    scope = WorkspaceScope(tenant_id, thread_id, scope_name or "thread")
    return ThreadWorkspace(
        scope=scope,
        manifest=name if scope_name is not None else None,
        # Off the event loop: a stat and a small state file on whatever disk the checkouts are on.
        root_kind="checkout"
        if await asyncio.to_thread(_has_checkout, settings, tenant_id, thread_id, scope.scope)
        else "scoped",
    )


async def _manifest_scope(
    settings: Settings, tenant_id: str, thread_id: str, name: str, *, strict: bool
) -> WorkspaceScopeName | None:
    from felix.runtime import resolve_tenant_manifest

    try:
        resolved = await resolve_tenant_manifest(settings, tenant_id, name, thread_id=thread_id)
    except LookupError, ValueError:
        if strict:
            raise UnknownManifest(name) from None
        # No caller-supplied value in the line: the name came from a request once, and a log
        # line is no place for whatever it held. Which thread is in the request's own log context.
        logger.info("workspace pane: the thread's manifest no longer resolves; using thread scope")
        return None
    return resolved.manifest.spec.workspace.scope


def _has_checkout(settings: Settings, tenant_id: str, thread_id: str, scope: WorkspaceScopeName) -> bool:
    """Whether the files are the thread's repository checkout.

    Locally a ready checkout replaces the scope's directory outright (`thread_workspace` returns
    its path). Under the hosted backend it is cloned into the thread's own sandbox, which is the
    `thread` scope's -- so it is what the pane sees only when the manifest's scope is `thread`.
    """
    from felix.repos.checkouts import READY, read_checkout
    from felix.tools.workspace import _thread_checkout_of

    if _thread_checkout_of(settings, tenant_id, thread_id) is not None:
        return True
    state = read_checkout(settings, tenant_id, thread_id)
    return bool(state and state.get("hosted") and state.get("state") == READY and scope == "thread")


__all__ = [
    "TREE_DEFAULT_LIMIT",
    "TREE_MAX_LIMIT",
    "RootKind",
    "ThreadWorkspace",
    "UnknownManifest",
    "resolve_thread_workspace",
]
