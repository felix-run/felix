"""`spec.github_publish` — publish commits already made in the workspace, from the harness.

The workspace is where repository code runs, so it holds no GitHub credential: the shell tool
cannot push, and `deploy/docker/compose.self.yml` keeps the bot's token out of every process
there. Publishing used to go through GitHub's `push_files` MCP tool, which carries *whole file
contents* as tool arguments — a one-line CHANGELOG entry put ~196 KiB into the model's context
and into the approval row that is meant to be the reviewable diff (#307).

`publish_commits(branch, head_sha)` takes a reference instead of the content. The harness reads
the commits with read-only git plumbing and writes them through GitHub's Git Data API:

1. The parent is the remote tip of `branch`, or of `base` when the branch does not exist yet.
   It must be an ancestor of `head_sha` in the workspace, so the push is a fast-forward and
   never drops a remote commit.
2. `git diff --raw parent head` lists what changed; each added or modified blob is created
   (`POST /git/blobs`), deletions become `sha: null` tree entries, and one tree is built on
   the parent's tree (`POST /git/trees`).
3. One commit with `parents=[parent]` (`POST /git/commits`), then the ref is created or
   fast-forwarded with `force: false`.

Two integrity checks come free from git being content-addressed. Every blob sha GitHub returns
must equal the local blob's, and the tree sha must equal `head_sha^{tree}` — so what lands on
the branch is byte-for-byte the tree of the commit the approval named. That is why `head_sha`
in the arguments is enough for an approval to bind the content: a different content is a
different sha, a different call signature, and a new approval.

Several local commits are published as **one** commit whose message is `title` (or the head
commit's subject) followed by the local subjects, oldest first; a single local commit keeps its
own message. Squashing keeps the API work to one tree, and the pull request is the unit a
person reviews anyway.

The token lives only in the `Authorization` header of this module's HTTP client. The git
subprocesses get a minimal environment with no credential in it, and system, global and hook
configuration switched off — the repository's own `.git/config` is agent-writable, and git
would otherwise run an fsmonitor, an external diff or a textconv filter it names.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field

from felix.manifests.schema import GithubPublishSpec, is_valid_branch_name
from felix.security.egress import safe_async_client
from felix.timeouts import DEFAULT_CONNECT_TIMEOUT_S
from felix.tools.errors import ToolErrorCode, tool_error_output
from felix.tools.types import Tool, ToolInput, ToolInvocationCtx, ToolOutput, define_tool_with_executor
from felix.tools.workspace import workspace_root

logger = logging.getLogger("felix.tools.github_publish")

TOOL_NAME = "publish_commits"
GITHUB_API = "https://api.github.com"
GITHUB_WEB = "https://github.com"

# Per publish. GitHub accepts far more; these bound what one approved call can move and what
# the harness holds in memory while it does (every blob is read whole, then base64'd).
MAX_FILES = 300
MAX_TOTAL_BYTES = 8 * 1024 * 1024
# The part of the approval preview that is the unified diff. `--stat` comes first and is never
# cut, so a truncated preview still names every file.
MAX_PREVIEW_BYTES = 32 * 1024
# Subjects listed in a squashed commit message.
MAX_LISTED_COMMITS = 50

_HTTP_TIMEOUT_S = 30.0
# The whole publish, every request and git call together.
_PUBLISH_TIMEOUT_S = 300.0
_GIT_TIMEOUT_S = 60.0
_GIT_OUTPUT_CAP = 4 * 1024 * 1024
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# Before any subcommand. The workspace's `.git/config` is writable by the agent, so anything
# in it that makes git execute a program is switched off here rather than trusted.
_GIT_PRELUDE = (
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.pager=cat",
    "-c",
    "color.ui=false",
)
# For the two diff invocations: no external diff driver, no textconv filter.
_DIFF_SAFE = ("--no-ext-diff", "--no-textconv", "--no-color", "--no-renames")


class PublishArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    branch: str = Field(min_length=1, max_length=200, description="Branch to create or fast-forward.")
    head_sha: str = Field(
        min_length=40,
        max_length=40,
        description="Full 40-hex sha of the local commit to publish (git rev-parse HEAD).",
    )
    title: str | None = Field(
        default=None,
        max_length=200,
        description="Commit subject when several local commits are published as one.",
    )


class PublishRefused(Exception):
    """A call the model can fix: wrong branch, unknown sha, stale base. The message says how."""


class PublishFailed(Exception):
    """Git or GitHub failed. The message is safe to show: it never carries the token."""


# --- git, read-only --------------------------------------------------------------------


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


async def _git_run(
    root: Path, *args: str, stdin: bytes | None = None, limit: int = _GIT_OUTPUT_CAP
) -> _GitResult:
    """Run git in `root` and return stdout up to `limit` bytes, killing it past that."""
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


async def _git(root: Path, *args: str, stdin: bytes | None = None, limit: int = _GIT_OUTPUT_CAP) -> bytes:
    res = await _git_run(root, *args, stdin=stdin, limit=limit)
    if res.truncated:
        raise PublishFailed(f"git {args[0]} produced more than {limit} bytes")
    if res.code != 0:
        raise PublishFailed(f"git {args[0]} failed ({res.code}): {res.err[:300]}")
    return res.out


async def _is_commit(root: Path, sha: str) -> bool:
    res = await _git_run(root, "cat-file", "-e", f"{sha}^{{commit}}")
    return res.code == 0


async def _rev(root: Path, spec: str) -> str:
    return (await _git(root, "rev-parse", "--verify", "--end-of-options", spec)).decode().strip()


@dataclass(slots=True)
class _Change:
    status: str
    mode: str
    sha: str
    path: str


def _parse_raw(raw: bytes) -> list[_Change]:
    """`git diff --raw -z --no-abbrev`: `:old new oldsha newsha S\\0path\\0` per entry."""
    parts = raw.split(b"\0")
    out: list[_Change] = []
    i = 0
    while i + 1 < len(parts) and parts[i]:
        meta = parts[i].decode("ascii").lstrip(":").split(" ")
        old_mode, new_mode, _old_sha, new_sha, status = meta[:5]
        try:
            path = parts[i + 1].decode("utf-8")
        except UnicodeDecodeError:
            raise PublishRefused("a changed path is not valid UTF-8; GitHub's API cannot carry it") from None
        status = status[:1]
        out.append(
            _Change(status=status, mode=old_mode if status == "D" else new_mode, sha=new_sha, path=path)
        )
        i += 2
    return out


# --- the plan: what a publish would do, computed without writing anything ------------


@dataclass(slots=True)
class _Plan:
    branch: str
    head: str
    parent: str
    branch_exists: bool
    subjects: list[str]
    changes: list[_Change]


class _GitHub:
    """The one place the token is used. Nothing here logs a header or echoes a request."""

    def __init__(self, client: Any, api_base: str, repo: str) -> None:
        self._client = client
        self._base = f"{api_base.rstrip('/')}/repos/{repo}"

    async def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
        import httpx

        try:
            resp = await self._client.request(method, self._base + path, json=body)
        except httpx.HTTPError as exc:
            # The type only: asyncio puts the dialled address in `ConnectError`.
            raise PublishFailed(f"GitHub request failed: {type(exc).__name__}") from None
        try:
            data = resp.json() if resp.content else None
        except ValueError:
            data = None
        return resp.status_code, data

    async def ok(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        status, data = await self.call(method, path, body)
        if status >= 300:
            message = str((data or {}).get("message") or "") if isinstance(data, dict) else ""
            hint = " (the token is missing a permission, or has expired)" if status in (401, 403) else ""
            raise PublishFailed(
                f"GitHub {method} {path.split('?')[0]} returned {status}{hint}: {message[:200]}"
            )
        return data

    async def branch_tip(self, branch: str) -> str | None:
        status, data = await self.call("GET", f"/git/ref/heads/{quote(branch, safe='/')}")
        if status == 404:
            return None
        if status >= 300 or not isinstance(data, dict):
            raise PublishFailed(f"GitHub could not read branch {branch!r} ({status})")
        obj = data.get("object") or {}
        # `git/ref/heads/x` answers with a *list* when `x` is only a prefix of real refs.
        sha = str(obj.get("sha") or "") if isinstance(obj, dict) else ""
        if data.get("ref") != f"refs/heads/{branch}" or not _SHA_RE.match(sha):
            return None
        return sha


def _check_args(args: ToolInput, spec: GithubPublishSpec) -> tuple[str, str]:
    branch = str(args.get("branch") or "").strip()
    head = str(args.get("head_sha") or "").strip().lower()
    if not branch.startswith(spec.branch_prefix):
        raise PublishRefused(f"branch must start with {spec.branch_prefix!r}")
    if branch == spec.base:
        raise PublishRefused(f"cannot publish to the base branch {spec.base!r}")
    if not is_valid_branch_name(branch):
        raise PublishRefused(f"not a valid branch name: {branch!r}")
    if not _SHA_RE.match(head):
        raise PublishRefused("head_sha must be the full 40-character sha (git rev-parse HEAD)")
    return branch, head


async def _plan(root: Path, gh: _GitHub, spec: GithubPublishSpec, args: ToolInput) -> _Plan:
    branch, head = _check_args(args, spec)
    if not await _is_commit(root, head):
        raise PublishRefused(f"{head} is not a commit in the workspace; commit first, then pass its sha")
    tip = await gh.branch_tip(branch)
    parent = tip or await gh.branch_tip(spec.base)
    if parent is None:
        raise PublishFailed(f"base branch {spec.base!r} does not exist on {spec.repo}")
    where = branch if tip else spec.base
    if not await _is_commit(root, parent):
        raise PublishRefused(
            f"the remote {where} is at {parent}, which the workspace does not have: "
            f'run ["git","fetch","origin"] and rebase onto origin/{where}, then commit and retry'
        )
    ancestor = await _git_run(root, "merge-base", "--is-ancestor", parent, head)
    if ancestor.code == 1:
        raise PublishRefused(
            f"{head[:12]} does not contain the remote {where} ({parent[:12]}); publishing it would "
            f"drop remote commits. Rebase onto origin/{where}, commit, and retry with the new sha"
        )
    if ancestor.code != 0:
        raise PublishFailed(f"git merge-base failed ({ancestor.code}): {ancestor.err[:300]}")
    log = await _git(root, "log", "--reverse", "--format=%s", f"{parent}..{head}", "--")
    subjects = [s for s in log.decode("utf-8", "replace").splitlines() if s.strip()]
    raw = await _git(root, "diff", "--raw", "-z", "--no-abbrev", *_DIFF_SAFE, parent, head, "--")
    changes = _parse_raw(raw)
    if len(changes) > MAX_FILES:
        raise PublishRefused(f"{len(changes)} files changed; one publish carries at most {MAX_FILES}")
    return _Plan(
        branch=branch,
        head=head,
        parent=parent,
        branch_exists=tip is not None,
        subjects=subjects,
        changes=changes,
    )


async def _preview_text(root: Path, plan: _Plan, repo: str) -> str:
    """What the approval row shows: header, commit list, `--stat`, then the diff, capped."""
    if plan.parent == plan.head:
        return f"nothing to publish: {repo}:{plan.branch} is already at {plan.head}"
    head = [
        f"publish {plan.head} to {repo}:{plan.branch} "
        f"({'fast-forward from' if plan.branch_exists else 'new branch from'} {plan.parent})",
        f"{len(plan.subjects)} local commit(s), published as one:",
        *(f"  {s}" for s in plan.subjects[:MAX_LISTED_COMMITS]),
        "",
    ]
    stat = await _git(root, "diff", "--stat=120", *_DIFF_SAFE, plan.parent, plan.head, "--")
    res = await _git_run(root, "diff", *_DIFF_SAFE, plan.parent, plan.head, "--", limit=MAX_PREVIEW_BYTES)
    if res.code != 0 and not res.truncated:
        raise PublishFailed(f"git diff failed ({res.code}): {res.err[:300]}")
    diff = res.out.decode("utf-8", "replace")
    tail = (
        f"\n[diff truncated at {MAX_PREVIEW_BYTES} bytes; the --stat above lists every file]\n"
        if res.truncated
        else ""
    )
    return "\n".join(head) + stat.decode("utf-8", "replace") + "\n" + diff + tail


def _message(plan: _Plan, title: str | None, single: str) -> str:
    if len(plan.subjects) <= 1:
        return single
    subject = (title or "").strip() or plan.subjects[-1]
    listed = [f"- {s}" for s in plan.subjects[:MAX_LISTED_COMMITS]]
    if len(plan.subjects) > MAX_LISTED_COMMITS:
        listed.append(f"- … and {len(plan.subjects) - MAX_LISTED_COMMITS} more")
    return subject + "\n\n" + "\n".join(listed) + "\n"


async def _blob_sizes(root: Path, shas: list[str]) -> dict[str, int]:
    if not shas:
        return {}
    out = await _git(root, "cat-file", "--batch-check", stdin=("\n".join(shas) + "\n").encode())
    sizes: dict[str, int] = {}
    for line in out.decode().splitlines():
        sha, kind, size = [*line.split(" "), "", ""][:3]
        if kind != "blob":
            raise PublishFailed(f"{sha} is not a blob in the workspace")
        sizes[sha] = int(size)
    return sizes


async def _publish(root: Path, gh: _GitHub, plan: _Plan, title: str | None) -> tuple[str, int]:
    uploads = [c for c in plan.changes if c.status != "D" and c.mode != "160000"]
    sizes = await _blob_sizes(root, sorted({c.sha for c in uploads}))
    total = sum(sizes.values())
    if total > MAX_TOTAL_BYTES:
        raise PublishRefused(f"{total} bytes changed; one publish carries at most {MAX_TOTAL_BYTES}")

    created: set[str] = set()
    for change in uploads:
        if change.sha in created:
            continue
        data = await _git(root, "cat-file", "blob", change.sha, limit=sizes[change.sha] + 1)
        blob = await gh.ok(
            "POST", "/git/blobs", {"content": base64.b64encode(data).decode("ascii"), "encoding": "base64"}
        )
        if (blob or {}).get("sha") != change.sha:
            raise PublishFailed(f"GitHub stored {change.path} as a different blob than the workspace holds")
        created.add(change.sha)

    # A deletion is the same entry with `sha: null`; its mode is the old one, which is what
    # `_parse_raw` records for a `D`. Mode 160000 is a submodule pointer, not a blob.
    tree_entries = [
        {
            "path": c.path,
            "mode": c.mode,
            "type": "commit" if c.mode == "160000" else "blob",
            "sha": None if c.status == "D" else c.sha,
        }
        for c in plan.changes
    ]
    base_tree = await _rev(root, f"{plan.parent}^{{tree}}")
    want_tree = await _rev(root, f"{plan.head}^{{tree}}")
    tree = await gh.ok("POST", "/git/trees", {"base_tree": base_tree, "tree": tree_entries})
    if (tree or {}).get("sha") != want_tree:
        # Nothing points at the tree yet, so stopping here leaves the branch untouched.
        raise PublishFailed(
            f"the tree GitHub built is not the tree of {plan.head[:12]}; refusing to commit it"
        )

    single = (await _git(root, "log", "-1", "--format=%B", plan.head, "--")).decode("utf-8", "replace")
    commit = await gh.ok(
        "POST",
        "/git/commits",
        {
            "message": _message(plan, title, single.rstrip("\n") + "\n"),
            "tree": want_tree,
            "parents": [plan.parent],
        },
    )
    new_sha = str((commit or {}).get("sha") or "")
    if not _SHA_RE.match(new_sha):
        raise PublishFailed("GitHub did not return a commit sha")

    ref = quote(plan.branch, safe="/")
    if plan.branch_exists:
        await gh.ok("PATCH", f"/git/refs/heads/{ref}", {"sha": new_sha, "force": False})
    else:
        await gh.ok("POST", "/git/refs", {"ref": f"refs/heads/{plan.branch}", "sha": new_sha})
    return new_sha, len(plan.changes)


class _PublishExecutor:
    # Not `local`: the result is written from a GitHub response, so it reaches content
    # screening like any other outbound tool's.
    transport = "github"

    def __init__(
        self,
        spec: GithubPublishSpec,
        *,
        token: str,
        allow_http: bool = False,
        api_base: str = GITHUB_API,
        web_base: str = GITHUB_WEB,
    ) -> None:
        self._spec = spec
        self._token = token
        self._allow_http = allow_http
        self._api_base = api_base
        self._web_base = web_base.rstrip("/")

    def _client(self) -> Any:
        import httpx

        return safe_async_client(
            allow_http=self._allow_http,
            timeout=httpx.Timeout(_HTTP_TIMEOUT_S, connect=DEFAULT_CONNECT_TIMEOUT_S),
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "felix-publish-commits",
            },
        )

    async def preview(self, args: ToolInput) -> str:
        """The approval preview: computed from `head_sha` in the workspace, never from the model."""
        root = workspace_root()
        async with self._client() as client:
            plan = await _plan(root, _GitHub(client, self._api_base, self._spec.repo), self._spec, args)
        return await _preview_text(root, plan, self._spec.repo)

    async def execute(self, args: ToolInput, ctx: ToolInvocationCtx | None = None) -> ToolOutput:
        _ = ctx
        try:
            root = workspace_root()
        except ValueError as exc:
            return tool_error_output(ToolErrorCode.TRANSPORT_UNAVAILABLE, str(exc))
        title = str(args.get("title") or "")[:200] or None
        try:
            async with asyncio.timeout(_PUBLISH_TIMEOUT_S), self._client() as client:
                gh = _GitHub(client, self._api_base, self._spec.repo)
                plan = await _plan(root, gh, self._spec, args)
                if plan.parent == plan.head:
                    return f"nothing to publish: {plan.branch} is already at {plan.head}"
                if not plan.changes:
                    raise PublishRefused(f"{plan.head[:12]} changes no files relative to {plan.parent[:12]}")
                sha, files = await _publish(root, gh, plan, title)
        except PublishRefused as exc:
            return tool_error_output(ToolErrorCode.INVALID_ARGUMENTS, f"[publish refused] {exc}")
        except PublishFailed as exc:
            logger.warning("publish_commits failed repo=%s: %s", self._spec.repo, exc)
            return tool_error_output(ToolErrorCode.PROVIDER_ERROR, f"[publish failed] {exc}")
        except TimeoutError:
            return tool_error_output(
                ToolErrorCode.TIMEOUT, f"[publish failed] timed out after {_PUBLISH_TIMEOUT_S:.0f}s"
            )
        compare = (
            f"{self._web_base}/{self._spec.repo}/compare/{self._spec.base}...{quote(plan.branch, safe='/')}"
        )
        return (
            f"published {files} file(s) to {self._spec.repo}:{plan.branch} as {sha} "
            f"({'fast-forward' if plan.branch_exists else 'new branch'}, parent {plan.parent})\n"
            f"compare: {compare}"
        )


def tool_from_github_publish(
    spec: GithubPublishSpec,
    *,
    token: str,
    allow_http: bool = False,
    api_base: str = GITHUB_API,
    web_base: str = GITHUB_WEB,
) -> Tool:
    """Bind `publish_commits`. `api_base`/`web_base` exist for tests; a manifest cannot set them."""
    if not token:
        raise ValueError("github_publish: the auth secret resolved to an empty token")
    executor = _PublishExecutor(
        spec, token=token, allow_http=allow_http, api_base=api_base, web_base=web_base
    )
    return define_tool_with_executor(
        name=TOOL_NAME,
        description=(
            f"Publish commits you made in the workspace to {spec.repo} on GitHub. Pass the branch "
            f"(must start with {spec.branch_prefix!r}) and the full sha from git rev-parse HEAD. "
            f"A new branch starts from {spec.base}; an existing one is fast-forwarded, so the "
            "commit must contain the remote tip — fetch and rebase first if it does not. Several "
            "local commits are published as one; title names it."
        ),
        args=PublishArgs,
        executor=executor,
        source="github",
        # Creates a commit and moves a ref. A resumed run re-issuing it after the ref moved
        # would find a new parent and publish again.
        replay_safe=False,
        approval_preview=executor.preview,
    )


__all__ = [
    "GITHUB_API",
    "MAX_FILES",
    "MAX_PREVIEW_BYTES",
    "MAX_TOTAL_BYTES",
    "TOOL_NAME",
    "PublishArgs",
    "tool_from_github_publish",
]
