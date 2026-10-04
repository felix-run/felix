"""GitHub as a source of Agent Skills: source syntax, the allowlist, and the four API calls.

A source is `github:owner/repo[/path]`; a ref (branch, tag or SHA) is separate and defaults to the
repository's default branch. The host is fixed -- every call goes to `api.github.com` through the
egress-pinned client (`security/egress.py`), so a caller names a repository and never a URL.

The sequence is Skillist's mirror sync, ported: the repository's metadata (default branch, SPDX
license) → `commits/{ref}`, resolving the ref to a commit SHA once → the recursive tree *at that
SHA* → blobs, by blob SHA. Nothing after the resolve names the ref again, so a branch that moves
mid-import cannot mix two commits into one version, and every blob is checked against its git
object id, so the bytes are the ones the tree named.

`skills/importer.py` builds a browse and an import on top of this; nothing here touches the
library.
"""

from __future__ import annotations

import base64
import binascii
import fnmatch
import hashlib
import json
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx

from felix.config import Settings
from felix.skills.format import SKILL_NAME_RE
from felix.skills.library import SkillLibraryError

GITHUB_API = "https://api.github.com"
GITHUB_PREFIX = "github:"

# Canonical skill roots a browse looks under, kept in step with Skillist's `DEFAULT_ROOTS` (and
# its CLI scanner), so a repository lists the same skills there and here.
DEFAULT_ROOTS: tuple[str, ...] = (
    ".cursor/skills",
    ".claude/skills",
    ".claude/plugins/marketplaces",
    ".agents/skills",
    ".gemini/skills",
    ".codex/skills",
    ".vscode/skills",
    "skills",
)

_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
_MAX_JSON_BYTES = 16 * 1024 * 1024
_MAX_PATH_SEGMENTS = 16

_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}\Z")
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}\Z")
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}\Z")
_REF_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}\Z")
_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
# A ref that may be a commit id, full or abbreviated, once no branch or tag has its name.
_COMMIT_ID_RE = re.compile(r"^[0-9a-f]{7,64}\Z")
_SPDX_RE = re.compile(r"^[A-Za-z0-9.+-]{1,64}\Z")
# Annotated tags may point at tags; this many hops, then it is refused.
_MAX_TAG_HOPS = 4

logger = logging.getLogger("felix.skills.github")


# -- refusals ----------------------------------------------------------------------------------


class SkillImportError(SkillLibraryError):
    """Base of every refusal an import or a browse makes; each subclass carries a stable code."""


class ImportSourceInvalid(SkillImportError):
    code = "invalid_source"


class ImportSourceNotAllowed(SkillImportError):
    code = "source_not_allowed"


class ImportSourceNotFound(SkillImportError):
    code = "source_not_found"


class ImportSourceTooLarge(SkillImportError):
    code = "source_too_large"


class ImportUpstreamError(SkillImportError):
    code = "upstream_error"


class ImportRateLimited(ImportUpstreamError):
    code = "upstream_rate_limited"


class ImportEgressBlocked(ImportUpstreamError):
    code = "egress_blocked"


class ImportTooRecent(SkillImportError):
    """Felix first saw these files more recently than the minimum import age allows: a cooldown
    so a compromised upstream commit has time to be noticed before anyone pulls it. Nothing
    overrides it; waiting does."""

    code = "too_recent"

    def __init__(self, message: str, *, first_seen_at: int, eligible_at: int) -> None:
        super().__init__(message)
        self.first_seen_at, self.eligible_at = first_seen_at, eligible_at


class ImportRefAmbiguous(SkillImportError):
    """A bare ref names both a tag and a branch. Spell it `refs/tags/<name>` or `refs/heads/<name>`."""

    code = "ambiguous_ref"


class ImportBudgetExhausted(SkillImportError):
    """The tenant's, or the deployment's, hourly budget of GitHub calls is spent. ``deployment``
    says which: a background sweep moves on to another tenant past a tenant's, and stops past the
    deployment's."""

    code = "rate_limited"

    def __init__(self, message: str, *, deployment: bool = False) -> None:
        super().__init__(message)
        self.deployment = deployment


class SkillNotImported(SkillImportError):
    """An upstream check or update named a skill whose newest version that was not rejected did
    not come from an import: there is no origin to check it against."""

    code = "not_imported"


class ImportCommitNotInRepo(SkillImportError):
    """The ref names a commit GitHub serves under this repository's name but that is not on its
    default branch -- one pushed only to a fork, say. The allowlist trusts a repository, not its
    fork network."""

    code = "commit_not_in_repo"


# -- sources -----------------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class GitHubSource:
    """A validated `github:owner/repo[/path]`. Owner and repository are lowercased -- GitHub
    treats them case-insensitively, and one skill must not have two origins by spelling."""

    owner: str
    repo: str
    path: str = ""

    @property
    def canonical(self) -> str:
        base = f"{GITHUB_PREFIX}{self.owner}/{self.repo}"
        return f"{base}/{self.path}" if self.path else base

    def at(self, path: str) -> GitHubSource:
        return GitHubSource(self.owner, self.repo, path)


def _segment_ok(segment: str) -> bool:
    return bool(_SEGMENT_RE.match(segment)) and segment not in {".", ".."}


def valid_source_path(path: str) -> bool:
    """Whether ``path`` may follow `github:owner/repo/` in a source: what `parse_source` accepts. A
    folder a tree names outside that grammar is never offered as one."""
    segments = path.split("/") if path else []
    return len(segments) <= _MAX_PATH_SEGMENTS and all(_segment_ok(s) for s in segments)


def parse_source(text: str) -> GitHubSource:
    """`github:owner/repo[/sub/path]`, every segment checked against what GitHub allows and what
    can be interpolated into an API path as-is: no `..`, no `%`, `?`, `#`, `\\` or whitespace."""
    raw = text.strip()
    if not raw.startswith(GITHUB_PREFIX):
        raise ImportSourceInvalid("a source is github:owner/repo[/path]")
    parts = raw[len(GITHUB_PREFIX) :].removesuffix("/").split("/")
    if len(parts) < 2:
        raise ImportSourceInvalid("a source names an owner and a repository: github:owner/repo[/path]")
    owner, repo, *rest = parts
    repo = repo.removesuffix(".git")
    if not _OWNER_RE.match(owner):
        raise ImportSourceInvalid(f"{owner!r} is not a GitHub owner name")
    if not _REPO_RE.match(repo) or repo in {".", ".."}:
        raise ImportSourceInvalid(f"{repo!r} is not a GitHub repository name")
    if len(rest) > _MAX_PATH_SEGMENTS or not all(_segment_ok(s) for s in rest):
        raise ImportSourceInvalid(
            "a source path is at most 16 segments of A-Z, a-z, 0-9, '.', '_' or '-', with no '.' or '..'"
        )
    return GitHubSource(owner.lower(), repo.lower(), "/".join(rest))


def validate_ref(ref: str) -> str:
    """A branch, tag or SHA as git would accept it, narrowed to what is safe in one URL segment
    once percent-encoded: no `..`, no empty or dot-leading component, no leading `-`."""
    if (
        not _REF_RE.match(ref)
        or ".." in ref
        or ref.startswith("-")
        or any(not part or part.startswith(".") or part.endswith(".lock") for part in ref.split("/"))
    ):
        raise ImportSourceInvalid(f"{ref!r} is not a git branch, tag or commit")
    return ref


@dataclass(slots=True, frozen=True)
class SourceGrant:
    """One `FELIX_SKILL_IMPORT_SOURCES` entry: a glob over canonical sources, and the tenant it is
    bound to (`acme=github:acme/*`), or None for an unbound entry any tenant may use."""

    pattern: str
    tenant: str | None = None

    def covers(self, canonical: str) -> bool:
        """A glob over the canonical source; one without a glob also covers everything under it:
        `github:myorg/skills` allows `github:myorg/skills/pdf`."""
        return fnmatch.fnmatchcase(canonical, self.pattern) or fnmatch.fnmatchcase(
            canonical, self.pattern.rstrip("/") + "/*"
        )

    @property
    def owner_is_literal(self) -> bool:
        """Whether the owner is spelled out rather than globbed: a token's reach is then bounded
        by an owner someone chose, not by whatever the token can read."""
        owner = self.pattern.removeprefix(GITHUB_PREFIX).split("/", 1)[0]
        return bool(owner) and not any(c in owner for c in "*?[]")


def parse_import_sources(raw: str) -> list[SourceGrant]:
    """`FELIX_SKILL_IMPORT_SOURCES`: comma-separated `[<tenant>=]github:<owner>[/<repo>[/<path>]]`,
    globs allowed. Raises ValueError on an entry that is not that shape, so a typo is a boot failure
    rather than an entry that silently matches nothing.

    The comma list of globs the setting already was, with an optional tenant in front -- rather
    than the JSON object `FELIX_GITHUB_ORG_TENANTS` is -- so an unbound list keeps its meaning."""
    from felix.auth.context import assert_valid_tenant_id

    grants: list[SourceGrant] = []
    for entry in (e.strip() for e in raw.split(",")):
        if not entry:
            continue
        tenant, sep, pattern = entry.partition("=")
        if not sep:
            tenant, pattern = "", entry
        tenant, pattern = tenant.strip(), pattern.strip().lower()
        owner = pattern.removeprefix(GITHUB_PREFIX).split("/", 1)[0]
        if not pattern.startswith(GITHUB_PREFIX) or not owner:
            raise ValueError(f"{entry!r} must be [<tenant>=]github:<owner>[/<repo>[/<path>]], globs allowed")
        if sep:
            try:
                assert_valid_tenant_id(tenant)
            except ValueError as exc:
                raise ValueError(f"{entry!r}: {tenant!r} is not a tenant id ({exc})") from exc
        grants.append(SourceGrant(pattern=pattern, tenant=tenant if sep else None))
    return grants


def check_allowed(settings: Settings, source: GitHubSource, tenant_id: str) -> None:
    """Refuse a source `FELIX_SKILL_IMPORT_SOURCES` does not cover for ``tenant_id``: an entry bound
    to the tenant, or an unbound one. An empty list covers every source -- which boot allows only
    where no token reaches anything private (`config._validate_skill_import`)."""
    grants = parse_import_sources(settings.skill_import_sources)
    if not grants:
        return
    canonical = source.canonical.lower()
    if any(g.covers(canonical) for g in grants if g.tenant in (None, tenant_id)):
        return
    raise ImportSourceNotAllowed(f"{source.canonical} is not a source this tenant may import from")


# -- the tree ----------------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class TreeEntry:
    path: str
    type: str
    sha: str
    size: int = 0


@dataclass(slots=True, frozen=True)
class DiscoveredSkill:
    slug: str
    source_path: str
    skill_md_path: str


def discover_skills(tree: Iterable[TreeEntry], roots: Sequence[str] = DEFAULT_ROOTS) -> list[DiscoveredSkill]:
    """Every directory holding a `SKILL.md` under one of ``roots``, by slug.

    Flat (`skills/{slug}/SKILL.md`) and nested plugin layouts (`plugins/{p}/skills/{slug}/SKILL.md`)
    both count: a root named anywhere as a path segment marks what is under it. A directory whose
    name is not a valid skill name is skipped, and the first of two with one slug wins.
    """
    found: list[DiscoveredSkill] = []
    seen: set[str] = set()
    for entry in tree:
        if entry.type != "blob" or not entry.path.endswith("/SKILL.md"):
            continue
        source_path = entry.path.removesuffix("/SKILL.md")
        under_root = any(source_path == r or source_path.startswith(f"{r}/") for r in roots)
        if not under_root and not any(f"/{r}/" in source_path for r in roots):
            continue
        slug = source_path.rsplit("/", 1)[-1]
        if not SKILL_NAME_RE.match(slug) or slug in seen:
            continue
        seen.add(slug)
        found.append(DiscoveredSkill(slug=slug, source_path=source_path, skill_md_path=entry.path))
    return sorted(found, key=lambda s: s.slug)


def skill_file_entries(tree: Iterable[TreeEntry], source_path: str) -> list[TreeEntry]:
    """The blobs under a skill folder, with paths relative to it. Dot-paths are skipped."""
    prefix = f"{source_path}/" if source_path else ""
    out: list[TreeEntry] = []
    for entry in tree:
        if entry.type != "blob" or not entry.path.startswith(prefix):
            continue
        relative = entry.path[len(prefix) :]
        if relative and not relative.startswith("."):
            out.append(TreeEntry(path=relative, type="blob", sha=entry.sha, size=entry.size))
    return out


def hash_tree_snapshot(entries: Iterable[TreeEntry]) -> str:
    """sha256 over the sorted `relativePath\\0blobSha` lines of a skill folder: what changes
    when, and only when, a file of the skill does."""
    lines = sorted(f"{e.path}\0{e.sha}" for e in entries)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


# -- GitHub ------------------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class RepoMeta:
    default_branch: str
    license: str | None


def github_client(settings: Settings) -> httpx.AsyncClient:
    """The production client: egress-pinned (`safe_async_client`), redirects not followed, and
    bounded by an explicit timeout."""
    from felix.security.egress import safe_async_client

    return safe_async_client(timeout=_TIMEOUT)


def _failure(resp: httpx.Response, what: str) -> SkillImportError:
    status = resp.status_code
    if status == 429 or (status == 403 and resp.headers.get("x-ratelimit-remaining") == "0"):
        reset = resp.headers.get("x-ratelimit-reset") or resp.headers.get("retry-after") or "unknown"
        return ImportRateLimited(
            f"GitHub's rate limit is exhausted (resets at {reset}); FELIX_SKILL_IMPORT_GITHUB_TOKEN raises it"
        )
    if status in {404, 409, 422}:
        # 409 is an empty repository, 422 a ref with no commit; a private repository read
        # without a token is a 404 as well.
        return ImportSourceNotFound(f"{what} was not found on GitHub (a private repository needs a token)")
    if status in {301, 302, 307, 308}:
        return ImportSourceNotFound(f"{what} has moved on GitHub; import it under its new name")
    if status == 401:
        return ImportUpstreamError("GitHub refused FELIX_SKILL_IMPORT_GITHUB_TOKEN (401)")
    return ImportUpstreamError(f"GitHub answered {status} for {what}")


async def _read_capped(resp: httpx.Response, limit: int, *, truncate: bool, what: str) -> bytes:
    """Read at most ``limit`` bytes of a streamed body, stopping there: refused past it, or with
    ``truncate`` cut off."""
    body = bytearray()
    async for chunk in resp.aiter_bytes():
        body.extend(chunk)
        if len(body) > limit:
            if truncate:
                return bytes(body[:limit])
            raise ImportSourceTooLarge(f"GitHub's answer for {what} is over {limit} bytes")
    return bytes(body)


class GitHubReader:
    """The GitHub calls an import makes. Every path is built from validated parts; the token,
    when there is one, goes in a header and nowhere else."""

    def __init__(
        self, http: httpx.AsyncClient, token: str = "", charge: Callable[[], Awaitable[None]] | None = None
    ) -> None:
        self._http = http
        self._token = token
        # Called before every request: the caller's budget of GitHub calls, which refuses
        # (`ImportBudgetExhausted`) once it is spent. The cost of a browse or an import is its
        # calls, not the request that asked for them.
        self._charge = charge

    def _headers(self, accept: str = "application/vnd.github+json") -> dict[str, str]:
        headers = {
            "Accept": accept,
            "User-Agent": "felix-skill-import",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    async def _get(
        self, path: str, *, what: str, limit: int, accept: str | None = None, truncate: bool = False
    ) -> bytes:
        """The body of one GET, at most ``limit`` bytes: past it, refused -- or, with
        ``truncate``, cut off there and the rest never read."""
        from felix.security.ssrf import EgressBlocked

        if self._charge is not None:
            await self._charge()
        headers = self._headers(accept) if accept else self._headers()
        try:
            async with self._http.stream("GET", f"{GITHUB_API}{path}", headers=headers) as resp:
                if resp.status_code != 200:
                    raise _failure(resp, what)
                return await _read_capped(resp, limit, truncate=truncate, what=what)
        except EgressBlocked as exc:
            raise ImportEgressBlocked(str(exc)) from None
        except httpx.TimeoutException:
            raise ImportUpstreamError(f"GitHub did not answer in time for {what}") from None
        except httpx.HTTPError as exc:
            raise ImportUpstreamError(f"could not reach GitHub for {what} ({type(exc).__name__})") from None

    async def _json(self, path: str, *, what: str, limit: int = _MAX_JSON_BYTES) -> dict[str, Any]:
        try:
            data = json.loads(await self._get(path, what=what, limit=limit))
        except ValueError:
            raise ImportUpstreamError(f"GitHub's answer for {what} is not JSON") from None
        if not isinstance(data, dict):
            raise ImportUpstreamError(f"GitHub's answer for {what} is not an object")
        return data

    def _repo_path(self, source: GitHubSource) -> str:
        return f"/repos/{source.owner}/{source.repo}"

    async def repo(self, source: GitHubSource) -> RepoMeta:
        data = await self._json(self._repo_path(source), what=f"{GITHUB_PREFIX}{source.owner}/{source.repo}")
        branch = data.get("default_branch")
        if not isinstance(branch, str):
            raise ImportUpstreamError("GitHub named no default branch")
        spdx = (data.get("license") or {}).get("spdx_id") if isinstance(data.get("license"), dict) else None
        license_id = (
            spdx if isinstance(spdx, str) and spdx != "NOASSERTION" and _SPDX_RE.match(spdx) else None
        )
        return RepoMeta(default_branch=validate_ref(branch), license=license_id)

    async def _named_ref(self, source: GitHubSource, kind: str, name: str) -> str | None:
        """The commit the repository's own branch or tag ``name`` points at, or None when it has
        no such ref. An annotated tag is followed to its commit."""
        try:
            data = await self._json(
                f"{self._repo_path(source)}/git/ref/{kind}/{quote(name, safe='/')}", what=f"ref {name!r}"
            )
        except ImportSourceNotFound:
            return None
        for _ in range(_MAX_TAG_HOPS):
            target = data.get("object")
            if not isinstance(target, dict):
                raise ImportUpstreamError(f"GitHub named no commit for {name!r}")
            sha, kind_of = target.get("sha"), target.get("type")
            if not isinstance(sha, str) or not _SHA_RE.match(sha):
                raise ImportUpstreamError(f"GitHub named no commit for {name!r}")
            if kind_of == "commit":
                return sha
            if kind_of != "tag":
                raise ImportSourceNotFound(f"{name!r} does not name a commit")
            data = await self._json(f"{self._repo_path(source)}/git/tags/{sha}", what=f"tag {name!r}")
        raise ImportUpstreamError(f"tag {name!r} nests deeper than {_MAX_TAG_HOPS} tags")

    async def _commit_by_sha(self, source: GitHubSource, ref: str) -> str:
        """The full SHA a (possibly abbreviated) commit id names. The `.sha` media type answers
        with the SHA alone, rather than a commit payload that carries every changed file's patch."""
        body = await self._get(
            f"{self._repo_path(source)}/commits/{quote(ref, safe='')}",
            what=f"commit {ref!r}",
            limit=256,
            accept="application/vnd.github.sha",
        )
        sha = body.decode("ascii", "replace").strip()
        if not _SHA_RE.match(sha):
            raise ImportUpstreamError(f"GitHub resolved {ref!r} to something that is not a commit SHA")
        return sha

    async def _reachable(self, source: GitHubSource, default_branch: str, sha: str) -> bool:
        """Whether ``sha`` is on the repository's default branch: its ancestor, or its tip."""
        try:
            data = await self._json(
                f"{self._repo_path(source)}/compare/{quote(default_branch, safe='/')}...{sha}?per_page=1",
                what=f"commit {sha[:12]}",
            )
        except ImportSourceNotFound, ImportSourceTooLarge:
            # A diff too large to answer is one with the commit far off the branch.
            return False
        return data.get("status") in {"behind", "identical"}

    async def commit(self, source: GitHubSource, ref: str, *, default_branch: str) -> str:
        """The full SHA ``ref`` names now -- provably *this* repository's commit.

        GitHub answers `commits/{sha}` for any commit in the repository's fork network, so a SHA
        pushed only to a fork would read as the upstream's, under the upstream's name and past an
        allowlist that trusts the upstream.

        - A commit id (hex, 7-64) is a commit and nothing else -- never looked up as a branch, so a
          branch someone names `deadbeef1234` cannot stand in for that commit -- and is accepted only
          when `compare` puts it on the default branch.
        - `refs/heads/<name>` and `refs/tags/<name>` resolve through the repository's own refs.
        - A bare name is looked up as a tag and as a branch, as git resolves one; naming both is
          refused as ambiguous rather than letting a branch shadow a release tag.
        """
        if _COMMIT_ID_RE.match(ref):
            sha = await self._commit_by_sha(source, ref)
            if not await self._reachable(source, default_branch, sha):
                raise ImportCommitNotInRepo(
                    f"commit {sha} is not on {source.owner}/{source.repo}'s {default_branch} branch; "
                    "import a branch or tag of the repository itself"
                )
            return sha
        for prefix, kind in (("refs/heads/", "heads"), ("refs/tags/", "tags")):
            if ref.startswith(prefix):
                sha = await self._named_ref(source, kind, ref.removeprefix(prefix))
                if sha is None:
                    raise ImportSourceNotFound(f"{source.owner}/{source.repo} has no {ref!r}")
                return sha
        tag = await self._named_ref(source, "tags", ref)
        head = await self._named_ref(source, "heads", ref)
        if tag is not None and head is not None:
            raise ImportRefAmbiguous(
                f"{ref!r} is both a tag and a branch of {source.owner}/{source.repo}; "
                f"name refs/tags/{ref} or refs/heads/{ref}"
            )
        sha = tag or head
        if sha is None:
            raise ImportSourceNotFound(f"{source.owner}/{source.repo} has no branch or tag {ref!r}")
        return sha

    async def last_changed(self, source: GitHubSource, commit: str, path: str) -> int | None:
        """When the newest commit touching ``path`` at ``commit`` says it was committed, in
        epoch ms, or None when GitHub gives no date.

        Provenance only. The committer date is whatever the pusher set, so nothing may decide
        on it: the import cooldown counts from when Felix first saw the skill's files
        (`sighting_store`)."""
        query = f"sha={commit}&per_page=1" + (f"&path={quote(path, safe='/')}" if path else "")
        try:
            data = json.loads(
                await self._get(
                    f"{self._repo_path(source)}/commits?{query}",
                    what=f"the history of {path}",
                    limit=1024 * 1024,
                )
            )
            stamp = data[0]["commit"]["committer"]["date"]
            return int(datetime.fromisoformat(stamp).timestamp() * 1000)
        except ValueError, LookupError, TypeError, ImportUpstreamError, ImportSourceNotFound:
            logger.info("no commit date for %s/%s:%s", source.owner, source.repo, path)
            return None

    async def tree(self, source: GitHubSource, commit: str) -> list[TreeEntry]:
        data = await self._json(
            f"{self._repo_path(source)}/git/trees/{commit}?recursive=1",
            what=f"the tree at {commit[:12]}",
        )
        if data.get("truncated"):
            # A partial listing would silently miss skills, or files of one.
            raise ImportSourceTooLarge(
                f"{GITHUB_PREFIX}{source.owner}/{source.repo} is too large for GitHub to list in one call"
            )
        entries = []
        for item in data.get("tree") or []:
            if not isinstance(item, dict) or item.get("type") not in {"blob", "tree"}:
                continue
            path, sha, size = item.get("path"), item.get("sha"), item.get("size")
            if isinstance(path, str) and isinstance(sha, str) and _SHA_RE.match(sha):
                entries.append(
                    TreeEntry(
                        path=path, type=item["type"], sha=sha, size=size if isinstance(size, int) else 0
                    )
                )
        return entries

    async def blob(self, source: GitHubSource, entry: TreeEntry) -> bytes:
        """One file's bytes, checked against its git object id: the tree named these bytes."""
        # base64 is 4/3 the size, with a newline every 60 characters, plus the JSON around it.
        limit = (entry.size + 2) // 3 * 4 * 61 // 60 + 64 * 1024
        data = await self._json(
            f"{self._repo_path(source)}/git/blobs/{entry.sha}", what=entry.path, limit=limit
        )
        content, encoding = data.get("content"), data.get("encoding")
        if not isinstance(content, str):
            raise ImportUpstreamError(f"GitHub sent no content for {entry.path}")
        try:
            raw = (
                base64.b64decode(content.replace("\n", ""), validate=True)
                if encoding == "base64"
                else content.encode("utf-8")
            )
        except binascii.Error, ValueError:
            raise ImportUpstreamError(f"GitHub sent undecodable content for {entry.path}") from None
        header = f"blob {len(raw)}\0".encode()
        digest = hashlib.sha1 if len(entry.sha) == 40 else hashlib.sha256
        if digest(header + raw, usedforsecurity=False).hexdigest() != entry.sha:
            raise ImportUpstreamError(f"{entry.path} does not match its git object id")
        return raw

    async def blob_head(self, source: GitHubSource, entry: TreeEntry, limit: int) -> bytes:
        """The first ``limit`` bytes of one file, raw, the rest never read: what a listing needs
        from a SKILL.md (its frontmatter) without holding the whole file. Not checked against
        its object id -- nothing is saved from it."""
        return await self._get(
            f"{self._repo_path(source)}/git/blobs/{entry.sha}",
            what=entry.path,
            limit=limit,
            accept="application/vnd.github.raw",
            truncate=True,
        )


@dataclass(slots=True, frozen=True)
class Resolved:
    source: GitHubSource
    ref: str
    commit: str
    license: str | None
    tree: list[TreeEntry]


@asynccontextmanager
async def reader(
    settings: Settings,
    http: httpx.AsyncClient | None,
    charge: Callable[[], Awaitable[None]] | None = None,
) -> AsyncIterator[GitHubReader]:
    """A reader over ``http`` (the caller's, left open), or over the production client, which is
    closed on the way out whatever happened inside."""
    if http is not None:
        yield GitHubReader(http, settings.skill_import_github_token, charge)
        return
    async with github_client(settings) as client:
        yield GitHubReader(client, settings.skill_import_github_token, charge)


async def resolve(gh: GitHubReader, source: GitHubSource, ref: str | None) -> Resolved:
    meta = await gh.repo(source)
    requested = ref or meta.default_branch
    commit = await gh.commit(source, requested, default_branch=meta.default_branch)
    return Resolved(source, requested, commit, meta.license, await gh.tree(source, commit))


__all__ = [
    "DEFAULT_ROOTS",
    "GITHUB_API",
    "GITHUB_PREFIX",
    "DiscoveredSkill",
    "GitHubReader",
    "GitHubSource",
    "ImportBudgetExhausted",
    "ImportCommitNotInRepo",
    "ImportEgressBlocked",
    "ImportRateLimited",
    "ImportRefAmbiguous",
    "ImportSourceInvalid",
    "ImportSourceNotAllowed",
    "ImportSourceNotFound",
    "ImportSourceTooLarge",
    "ImportTooRecent",
    "ImportUpstreamError",
    "RepoMeta",
    "Resolved",
    "SkillImportError",
    "SkillNotImported",
    "SourceGrant",
    "TreeEntry",
    "check_allowed",
    "discover_skills",
    "github_client",
    "hash_tree_snapshot",
    "parse_import_sources",
    "parse_source",
    "reader",
    "resolve",
    "skill_file_entries",
    "valid_source_path",
    "validate_ref",
]
