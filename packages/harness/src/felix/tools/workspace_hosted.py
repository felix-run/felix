"""The `hosted` workspace backend: each scope's files in its own sandbox, through the gateway.

With `FELIX_WORKSPACE_BACKEND=hosted`, the five workspace tools' file operations go to the workspace
gateway Worker (`deploy/cloudflare/workspace-gateway`) over HTTPS, and run there in the scope's
sandbox -- a microVM with the internet off, holding nothing of this process's -- with the local
backend's rules (`felix-fs`, ported from `workspace.py` and held to it by a test). Nothing a model
writes touches this host.

- **The scope names the sandbox.** `scope_relpath` (the same function the local layout uses)
  turns `(tenant, thread, spec.workspace.scope)` into `tenant/key`, and the gateway derives the
  sandbox from that and nothing else. A call with no thread under `thread` scope is refused here,
  exactly as locally.
- **`deployment` stays local.** It is the operator's whole root, by definition a directory on this
  host; it is honoured only for FELIX_WORKSPACE_DEPLOYMENT_TENANTS either way.
- **A thread with its own repository checkout is refused.** The checkout is on this host, which is
  what `hosted` exists to keep a model away from.
- **Failures arrive as the exceptions the tools already map.** The gateway answers each refusal with
  a code, and each code becomes the exception `LocalBackend` would have raised for the same cause,
  so a tool's message and error code are the same on both backends. A gateway that cannot be reached,
  refuses the token or answers anything unexpected is the workspace being unavailable
  (`workspace_root: ...`), which the tools report as `transport_unavailable`.
- **Files persist past the sandbox.** A successful write or edit records its scope on the request;
  when the request ends (`felix.context.async_run_with_context`), each written scope is
  checkpointed to R2 (`checkpoint_written`). The gateway also checkpoints a written scope before
  its idle stop, so a run that ends without reaching this loses nothing to idling.
"""

from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from felix.tools.workspace import NotAFileError
from felix.tools.workspace_backend import (
    EditRefused,
    EditResult,
    ListResult,
    ReadResult,
    SearchResult,
    WorkspaceScope,
    WriteResult,
)

if TYPE_CHECKING:
    from felix.config import Settings

logger = logging.getLogger("felix.tools.workspace_hosted")

# Per HTTP call. The gateway's own operation timeout is 30s and a cold start a few seconds more; a
# restore of a large workspace is bounded on the gateway side.
_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
# Where a request records the scopes it wrote, for `checkpoint_written`.
WRITTEN_SCOPES_KEY = "workspace_written_scopes"


def gateway_client(settings: Settings) -> httpx.AsyncClient:
    """The client every gateway call uses. Tests replace this one seam with a fake gateway."""
    return httpx.AsyncClient(timeout=_TIMEOUT)


class GatewayUnavailable(ValueError):
    """The hosted workspace cannot be reached or refused us: worded "workspace_root: ..." so the
    tools report it as the workspace being unavailable, not as a bad argument."""


def _unavailable(detail: str) -> GatewayUnavailable:
    return GatewayUnavailable(f"workspace_root: the hosted workspace is unavailable ({detail})")


def _os_error(kind: str, message: str) -> OSError:
    """The built-in `OSError` the helper named, so a tool words it as it would locally. Only an
    `OSError` subclass from the builtins: a gateway answer names a type, it does not choose code."""
    import builtins

    cls = getattr(builtins, kind, None)
    if isinstance(cls, type) and issubclass(cls, OSError):
        return cls(message)
    return OSError(message)


def _raise_for(code: str, message: str, kind: str = "") -> None:
    """The exception `LocalBackend` raises for the same cause."""
    match code:
        case "invalid_path":
            raise ValueError(message)
        case "not_found":
            raise FileNotFoundError(message)
        case "not_a_directory":
            raise NotADirectoryError(message)
        case "not_a_file":
            raise NotAFileError(message)
        case "edit_refused":
            raise EditRefused(message)
        case "permission_denied":
            raise PermissionError(message)
        case "io_error":
            raise _os_error(kind, message)
        case "timeout":
            raise TimeoutError(message)
        case "unauthorized":
            raise _unavailable("the gateway refused FELIX_WORKSPACE_GATEWAY_TOKEN")
        case _:
            raise _unavailable(f"{code}: {message}")


def gateway_path(settings: Settings, scope: WorkspaceScope) -> str:
    """`tenant/key` for a non-deployment scope. Raises ValueError as `scope_relpath` does."""
    from felix.tools.workspace_scope import scope_relpath

    rel = scope_relpath(settings, scope.tenant_id, scope.thread_id, scope.scope)
    _, tenant, key = rel.split("/")
    return f"{tenant}/{key}"


class HostedBackend:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._url = settings.workspace_gateway_url.strip().rstrip("/")

    def _local(self) -> Any:
        from felix.tools.workspace_local import LocalBackend

        return LocalBackend(self._settings)

    def _is_local(self, scope: WorkspaceScope | None) -> bool:
        return scope is None or scope.scope == "deployment"

    def _refuse_checkout(self, scope: WorkspaceScope) -> None:
        from felix.tools.workspace import _thread_checkout_of

        if _thread_checkout_of(self._settings, scope.tenant_id, scope.thread_id) is not None:
            raise ValueError(
                "workspace_root: this thread works in a repository checkout on the host, which the "
                "hosted workspace backend does not serve"
            )

    async def _call(
        self, scope: WorkspaceScope, op: str, body: dict[str, Any], timeout: httpx.Timeout = _TIMEOUT
    ) -> dict[str, Any]:
        self._refuse_checkout(scope)
        return await self._post(gateway_path(self._settings, scope), op, body, timeout)

    async def _post(
        self, path: str, op: str, body: dict[str, Any], timeout: httpx.Timeout = _TIMEOUT
    ) -> dict[str, Any]:
        """One gateway call for the scope `path` (`tenant/key`)."""
        url = f"{self._url}/v1/workspaces/{path}/{op}"
        headers = {"authorization": f"Bearer {self._settings.workspace_gateway_token}"}
        try:
            async with gateway_client(self._settings) as client:
                resp = await client.post(url, json=body, headers=headers, timeout=timeout)
        except httpx.TimeoutException as exc:
            raise _unavailable("the gateway did not answer in time") from exc
        except httpx.HTTPError as exc:
            raise _unavailable(f"the gateway could not be reached: {type(exc).__name__}") from exc
        try:
            answer = resp.json()
        except ValueError:
            answer = None
        if not isinstance(answer, dict):
            raise _unavailable(f"the gateway answered {resp.status_code} with no JSON object")
        if resp.status_code == 200 and isinstance(answer.get("result"), dict):
            return answer["result"]
        _raise_for(str(answer.get("error", "")), str(answer.get("message", "")), str(answer.get("kind", "")))
        raise AssertionError("unreachable")

    def _written(self, scope: WorkspaceScope) -> None:
        from felix.context import try_get_context

        ctx = try_get_context()
        if ctx is not None:
            ctx.extras.setdefault(WRITTEN_SCOPES_KEY, set()).add(scope)

    async def prepare(self, scope: WorkspaceScope | None) -> None:
        if self._is_local(scope):
            await self._local().prepare(scope)
            return
        assert scope is not None
        await self._call(scope, "prepare", {})

    async def list_dir(self, scope: WorkspaceScope | None, path: str) -> ListResult:
        if self._is_local(scope):
            return await self._local().list_dir(scope, path)
        assert scope is not None
        out = await self._call(scope, "list", {"path": path})
        return ListResult(path=str(out["path"]), entries=list(out["entries"]))

    async def read_file(self, scope: WorkspaceScope | None, path: str, offset: int, limit: int) -> ReadResult:
        if self._is_local(scope):
            return await self._local().read_file(scope, path, offset, limit)
        assert scope is not None
        out = await self._call(scope, "read", {"path": path, "offset": offset, "limit": limit})
        return ReadResult(path=str(out["path"]), size=int(out["size"]), data=base64.b64decode(out["data"]))

    async def write_file(
        self, scope: WorkspaceScope | None, path: str, data: bytes, append: bool
    ) -> WriteResult:
        if self._is_local(scope):
            return await self._local().write_file(scope, path, data, append)
        assert scope is not None
        body = {"path": path, "data": base64.b64encode(data).decode("ascii"), "append": append}
        out = await self._call(scope, "write", body)
        self._written(scope)
        return WriteResult(path=str(out["path"]), bytes=int(out["bytes"]))

    async def edit_file(
        self, scope: WorkspaceScope | None, path: str, old: str, new: str, replace_all: bool
    ) -> EditResult:
        if self._is_local(scope):
            return await self._local().edit_file(scope, path, old, new, replace_all)
        assert scope is not None
        body = {"path": path, "old": old, "new": new, "replace_all": replace_all}
        out = await self._call(scope, "edit", body)
        self._written(scope)
        return EditResult(
            path=str(out["path"]), replacements=int(out["replacements"]), bytes=int(out["bytes"])
        )

    async def search(
        self, scope: WorkspaceScope | None, path: str, query: str, regex: bool, max_hits: int
    ) -> SearchResult:
        if self._is_local(scope):
            return await self._local().search(scope, path, query, regex, max_hits)
        assert scope is not None
        body = {"path": path, "query": query, "regex": regex, "max_hits": max_hits}
        out = await self._call(scope, "search", body)
        return SearchResult(hits=list(out["hits"]))

    async def exec(
        self, scope: WorkspaceScope, argv: list[str], cwd: str, stdin: str | None, timeout_ms: int
    ) -> dict[str, Any]:
        """A `shell_tools` command in the scope's sandbox (3b). The result is the shell runner's
        shape, which the shell tool validates as it validates a runner's. A command may write, so
        the scope is checkpointed at the end of the request like any other written scope."""
        body: dict[str, Any] = {"argv": argv, "cwd": cwd, "timeout_ms": timeout_ms}
        if stdin is not None:
            body["stdin"] = stdin
        # The helper kills at the command's timeout and drains; a cold start comes on top.
        wait = httpx.Timeout(timeout_ms / 1000 + 90.0, connect=10.0)
        out = await self._call(scope, "exec", body, timeout=wait)
        self._written(scope)
        return out

    async def checkpoint(self, scope: WorkspaceScope) -> dict[str, Any]:
        return await self._call(scope, "checkpoint", {})


async def checkpoint_written(ctx: Any) -> None:
    """Back up every scope this request wrote. Called as the request ends; never raises.

    A failure is logged and left to the gateway's own idle backup, which covers a written scope
    before its sandbox stops: the request has already answered, and failing it now would report a
    finished run as broken over a copy that will still be made.
    """
    scopes = ctx.extras.pop(WRITTEN_SCOPES_KEY, None)
    if not scopes or getattr(ctx.settings, "workspace_backend", "local") != "hosted":
        return
    backend = HostedBackend(ctx.settings)
    for scope in scopes:
        try:
            await backend.checkpoint(scope)
        except Exception:
            logger.warning(
                "workspace checkpoint failed for %s; the gateway's idle backup covers it",
                scope,
                exc_info=True,
            )


@dataclass
class UploadReport:
    """What `upload_local_scopes` did for one scope (`tenant/key`)."""

    scope: str
    uploaded: list[str] = field(default_factory=list)
    # (path, why): a symlink is never followed and never recreated; a file over the write cap
    # cannot be sent in one write; anything unreadable is named rather than dropped silently.
    skipped: list[tuple[str, str]] = field(default_factory=list)
    checkpointed: bool = False


async def upload_local_scopes(
    settings: Settings, *, tenant: str | None = None, dry_run: bool = False
) -> list[UploadReport]:
    """Copy each local scope's files into its hosted sandbox, once, and back each one up.

    The local layout is `<root>/.felix-scopes/<tenant>/<key>`, and a scope's sandbox is named by
    the same `tenant/key`, so the copy needs no thread ids -- which the key, a hash, could not give
    back anyway. Files go through the gateway's ordinary `write` (the helper's rules apply on the
    way in), so a file the tools could not have written is skipped and reported, never forced: over
    the write cap, or a symlink. What is already in the sandbox under the same path is
    overwritten; nothing else there is touched. The local files stay where they are.
    """
    from felix.tools.workspace import _MAX_WRITE_BYTES, deployment_workspace
    from felix.tools.workspace_scope import SCOPES_DIR, is_scope_relpath

    base = deployment_workspace(settings.workspace_root) / SCOPES_DIR
    backend = HostedBackend(settings)
    reports: list[UploadReport] = []
    if not base.is_dir():
        return reports
    for tenant_dir in sorted(p for p in base.iterdir() if p.is_dir() and not p.is_symlink()):
        if tenant is not None and tenant_dir.name != tenant:
            continue
        for key_dir in sorted(p for p in tenant_dir.iterdir() if p.is_dir() and not p.is_symlink()):
            path = f"{tenant_dir.name}/{key_dir.name}"
            if not is_scope_relpath(f"{SCOPES_DIR}/{path}"):
                continue
            report = UploadReport(scope=path)
            reports.append(report)
            for dirpath, dirnames, filenames in os.walk(key_dir, followlinks=False):
                here = Path(dirpath)
                for name in sorted(filenames) + sorted(d for d in dirnames if (here / d).is_symlink()):
                    file = here / name
                    rel = file.relative_to(key_dir).as_posix()
                    if file.is_symlink():
                        report.skipped.append((rel, "a symlink"))
                        continue
                    try:
                        data = file.read_bytes()
                    except OSError as exc:
                        report.skipped.append((rel, type(exc).__name__))
                        continue
                    if len(data) > _MAX_WRITE_BYTES:
                        report.skipped.append((rel, f"over {_MAX_WRITE_BYTES} bytes"))
                        continue
                    if not dry_run:
                        body = {"path": rel, "data": base64.b64encode(data).decode("ascii"), "append": False}
                        await backend._post(path, "write", body)
                    report.uploaded.append(rel)
                dirnames[:] = [d for d in dirnames if not (here / d).is_symlink()]
            if report.uploaded and not dry_run:
                await backend._post(path, "checkpoint", {})
                report.checkpointed = True
    return reports


def local_only_refusal(scope_name: str) -> str:
    """The message for a consumer that needs a directory on this host, under `hosted`."""
    return (
        f"workspace_root: this tool needs a directory on the host, and under the hosted workspace "
        f"backend only `deployment` scope has one (this agent's scope is `{scope_name}`)"
    )


__all__ = [
    "WRITTEN_SCOPES_KEY",
    "GatewayUnavailable",
    "HostedBackend",
    "UploadReport",
    "checkpoint_written",
    "gateway_client",
    "gateway_path",
    "local_only_refusal",
    "upload_local_scopes",
]
