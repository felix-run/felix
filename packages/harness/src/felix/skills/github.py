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
import re
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
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
_SPDX_RE = re.compile(r"^[A-Za-z0-9.+-]{1,64}\Z")


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


def allowed_patterns(settings: Settings) -> list[str]:
    return [p.strip().lower() for p in settings.skill_import_sources.split(",") if p.strip()]


def check_allowed(settings: Settings, source: GitHubSource) -> None:
    """Refuse a source `FELIX_SKILL_IMPORT_SOURCES` does not cover. Empty covers every GitHub
    source. An entry is a glob over the canonical source, and one without a glob also covers
    everything under it: `github:myorg/skills` allows `github:myorg/skills/pdf`."""
    patterns = allowed_patterns(settings)
    if not patterns:
        return
    canonical = source.canonical.lower()
    for pattern in patterns:
        if fnmatch.fnmatchcase(canonical, pattern) or fnmatch.fnmatchcase(
            canonical, pattern.rstrip("/") + "/*"
        ):
            return
    raise ImportSourceNotAllowed(f"{source.canonical} is not a source FELIX_SKILL_IMPORT_SOURCES allows")


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


class GitHubReader:
    """The four GitHub calls an import makes. Every path is built from validated parts; the
    token, when there is one, goes in a header and nowhere else."""

    def __init__(self, http: httpx.AsyncClient, token: str = "") -> None:
        self._http = http
        self._token = token

    def _headers(self, accept: str = "application/vnd.github+json") -> dict[str, str]:
        headers = {
            "Accept": accept,
            "User-Agent": "felix-skill-import",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    async def _get(self, path: str, *, what: str, limit: int, accept: str | None = None) -> bytes:
        from felix.security.ssrf import EgressBlocked

        headers = self._headers(accept) if accept else self._headers()
        try:
            async with self._http.stream("GET", f"{GITHUB_API}{path}", headers=headers) as resp:
                if resp.status_code != 200:
                    raise _failure(resp, what)
                body = bytearray()
                async for chunk in resp.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > limit:
                        raise ImportSourceTooLarge(f"GitHub's answer for {what} is over {limit} bytes")
                return bytes(body)
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

    async def repo(self, source: GitHubSource) -> RepoMeta:
        data = await self._json(
            f"/repos/{source.owner}/{source.repo}", what=f"{GITHUB_PREFIX}{source.owner}/{source.repo}"
        )
        branch = data.get("default_branch")
        if not isinstance(branch, str):
            raise ImportUpstreamError("GitHub named no default branch")
        spdx = (data.get("license") or {}).get("spdx_id") if isinstance(data.get("license"), dict) else None
        license_id = (
            spdx if isinstance(spdx, str) and spdx != "NOASSERTION" and _SPDX_RE.match(spdx) else None
        )
        return RepoMeta(default_branch=validate_ref(branch), license=license_id)

    async def commit(self, source: GitHubSource, ref: str) -> str:
        """The full SHA ``ref`` names now. The `.sha` media type answers with the SHA alone,
        rather than a commit payload that carries every changed file's patch."""
        body = await self._get(
            f"/repos/{source.owner}/{source.repo}/commits/{quote(ref, safe='')}",
            what=f"ref {ref!r}",
            limit=256,
            accept="application/vnd.github.sha",
        )
        sha = body.decode("ascii", "replace").strip()
        if not _SHA_RE.match(sha):
            raise ImportUpstreamError(f"GitHub resolved {ref!r} to something that is not a commit SHA")
        return sha

    async def tree(self, source: GitHubSource, commit: str) -> list[TreeEntry]:
        data = await self._json(
            f"/repos/{source.owner}/{source.repo}/git/trees/{commit}?recursive=1",
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
            f"/repos/{source.owner}/{source.repo}/git/blobs/{entry.sha}", what=entry.path, limit=limit
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


@dataclass(slots=True, frozen=True)
class Resolved:
    source: GitHubSource
    ref: str
    commit: str
    license: str | None
    tree: list[TreeEntry]


@asynccontextmanager
async def reader(settings: Settings, http: httpx.AsyncClient | None) -> AsyncIterator[GitHubReader]:
    """A reader over ``http`` (the caller's, left open), or over the production client."""
    if http is not None:
        yield GitHubReader(http, settings.skill_import_github_token)
        return
    async with github_client(settings) as client:
        yield GitHubReader(client, settings.skill_import_github_token)


async def resolve(gh: GitHubReader, source: GitHubSource, ref: str | None) -> Resolved:
    meta = await gh.repo(source)
    requested = ref or meta.default_branch
    commit = await gh.commit(source, requested)
    return Resolved(source, requested, commit, meta.license, await gh.tree(source, commit))


__all__ = [
    "DEFAULT_ROOTS",
    "GITHUB_API",
    "GITHUB_PREFIX",
    "DiscoveredSkill",
    "GitHubReader",
    "GitHubSource",
    "ImportEgressBlocked",
    "ImportRateLimited",
    "ImportSourceInvalid",
    "ImportSourceNotAllowed",
    "ImportSourceNotFound",
    "ImportSourceTooLarge",
    "ImportUpstreamError",
    "RepoMeta",
    "Resolved",
    "SkillImportError",
    "TreeEntry",
    "allowed_patterns",
    "check_allowed",
    "discover_skills",
    "github_client",
    "hash_tree_snapshot",
    "parse_source",
    "reader",
    "resolve",
    "skill_file_entries",
    "validate_ref",
]
