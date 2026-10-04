"""Import Agent Skills from GitHub into the tenant library, as drafts, and browse what a
repository offers.

The GitHub half -- source syntax, the allowlist, the pinned fetch -- is `skills/github.py`. This
is what the library makes of it.

What lands is a draft (`library.save_draft`, `source="import"`), never a live skill: publishing
is a separate step a person takes after review, through the same gate an operator's upload
passes -- and an imported version, and every version built on one, is held to a stricter one
(`publish_gate.gate_source`, `policy_for_source`: an advisory scan blocks).

The bundle is sanitised before it is validated, as Skillist's mirror does: what the bundle format
accepts is kept (`format.bundle_path_issue`) less `evals/` and dot-paths, everything else
(examples, tests, a LICENSE file) is dropped and reported, and an over-long description is clamped
(`format.clamp_description`). The skill's name must be its folder's.

Change detection is a digest of the kept files' tree entries (`github.hash_tree_snapshot`), not
the commit SHA: a repository commits constantly, and an unrelated commit -- or a change to a file
the import drops -- must not mint a new version. A re-import whose digest matches the newest
version's saves nothing (`unchanged`).

The cooldown counts from when this tenant first saw the digest (`sighting_store`), recorded on
every browse and import attempt, never from a commit date the pusher chose. Every GitHub call is
charged to the tenant's and the deployment's hourly budget (`github_call_budget`).

An import never takes over a name: if the newest version that was not rejected came from another
source, or an agent or an operator wrote it, the import is refused (`origin_mismatch`); and a
name an operator upload holds is refused too (`library.save_draft`).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from felix.config import Settings
from felix.skills import library
from felix.skills.binary import (
    MAX_BINARY_ASSET_BYTES,
    base64_encoded_size,
    encode_base64,
    is_binary_asset_path,
)
from felix.skills.format import (
    ALLOWED_ROOT_FILES,
    BUNDLE_DIRS,
    MAX_BUNDLE_BYTES,
    MAX_BUNDLE_FILES,
    MAX_DESCRIPTION_CHARS,
    bundle_path_issue,
    clamp_description,
    parse_skill_md,
)
from felix.skills.github import (
    DEFAULT_ROOTS,
    DiscoveredSkill,
    GitHubReader,
    GitHubSource,
    ImportBudgetExhausted,
    ImportSourceNotFound,
    ImportSourceTooLarge,
    ImportTooRecent,
    ImportUpstreamError,
    Resolved,
    TreeEntry,
    check_allowed,
    discover_skills,
    hash_tree_snapshot,
    parse_source,
    reader,
    resolve,
    skill_file_entries,
    valid_source_path,
    validate_ref,
)
from felix.skills.library_store import get_skill_library_store
from felix.skills.sighting_store import get_sighting_store

logger = logging.getLogger("felix.skills.importer")

# What an import keeps: the bundle layout less `evals/`. A bundle's evaluation scenarios count
# toward the publish gate only when an operator wrote them (`publish_gate.eval_counts_for_gate`),
# so a third party's are dropped rather than carried as if they were.
KEPT_DIRS = frozenset(BUNDLE_DIRS) - {"evals"}
# Skills one browse lists and reads a SKILL.md head for: each costs a GitHub call on the
# deployment's token. A repository with more is listed in part (`truncated`); name a path.
MAX_BROWSE_SKILLS = 50
BROWSE_SKILL_MD_BYTES = 64 * 1024
# Wall clock for one whole import or browse, every GitHub call included.
DEADLINE_SECONDS = 120.0
_FETCH_CONCURRENCY = 8
DAY_MS = 86_400_000
HOUR_S = 3600

now_ms = lambda: int(time.time() * 1000)


@dataclass(slots=True, frozen=True)
class ImportDeps:
    """What an import or a browse reaches outside itself, each replaceable in a test.

    ``http``: a client for GitHub, left open; None is the egress-pinned production one
    (`github.github_client`). ``clock``: now, in epoch ms. ``object_store``: the library's bytes.
    ``charge``: called before every GitHub call (`github_call_budget`); None charges nothing."""

    http: httpx.AsyncClient | None = None
    clock: Callable[[], int] = now_ms
    object_store: Any | None = None
    charge: Callable[[], Awaitable[None]] | None = None


def github_call_budget(limiter: Any, settings: Settings, tenant_id: str) -> Callable[[], Awaitable[None]]:
    """A `charge` that spends one call from the tenant's hourly budget, then the deployment's.

    Per call rather than per request: a browse of fifty skills is fifty-odd calls on the shared
    token, and an import one per file. The tenant's bucket first, so a tenant refused there spends
    nothing from the one every tenant shares; the shared one protects the token's own GitHub limit
    from many tenants together."""

    async def charge() -> None:
        if not await limiter.hit(
            f"skill-import:{tenant_id}", limit=settings.skill_import_calls_per_hour, window_seconds=HOUR_S
        ):
            raise ImportBudgetExhausted(
                f"this tenant has spent its {settings.skill_import_calls_per_hour} GitHub calls this hour"
            )
        if not await limiter.hit(
            "skill-import:*", limit=settings.skill_import_calls_per_hour_total, window_seconds=HOUR_S
        ):
            raise ImportBudgetExhausted("this server has spent its GitHub calls for skill imports this hour")

    return charge


async def _gather_limited[T](calls: Iterable[Callable[[], Awaitable[T]]]) -> list[T]:
    """Run each call, at most `_FETCH_CONCURRENCY` at once, in order. A task group, so the first
    failure cancels the rest rather than leaving them fetching for an import already refused."""
    gate = asyncio.Semaphore(_FETCH_CONCURRENCY)

    async def one(call: Callable[[], Awaitable[T]]) -> T:
        async with gate:
            return await call()

    try:
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(one(c)) for c in calls]
    except* Exception as failed:
        # The first refusal is the one the caller gets, as a gather would have raised it.
        raise failed.exceptions[0] from None
    return [t.result() for t in tasks]


# -- the cooldown --------------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class Cooldown:
    """The minimum import age in force for a tenant (`FELIX_SKILL_IMPORT_MIN_AGE_DAYS`, tightened by
    the tenant's policy row), and the moment it is judged at."""

    days: int
    now: int

    def eligible_at(self, first_seen_at: int) -> int:
        return first_seen_at + self.days * DAY_MS

    def eligible(self, first_seen_at: int) -> bool:
        """At exactly the boundary it is old enough."""
        return self.now >= self.eligible_at(first_seen_at)

    def check(self, what: str, first_seen_at: int) -> None:
        """Refuse ``what`` when this tenant first saw its files less than ``days`` ago. A hard
        refusal: nothing is saved, and no flag overrides it."""
        if self.days and not self.eligible(first_seen_at):
            eligible_at = self.eligible_at(first_seen_at)
            raise ImportTooRecent(
                f"{what} was first seen {_iso(first_seen_at)}; the minimum import age is {self.days} "
                f"days, so these files can be imported from {_iso(eligible_at)}",
                first_seen_at=first_seen_at,
                eligible_at=eligible_at,
            )


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat(timespec="seconds")


@dataclass(slots=True)
class _Session:
    source: GitHubSource
    ref: str | None
    cooldown: Cooldown
    gh: GitHubReader


@asynccontextmanager
async def _github_session(
    settings: Settings, tenant_id: str, source: str, ref: str | None, deps: ImportDeps
) -> AsyncIterator[_Session]:
    """One browse's or import's checked request, cooldown and GitHub reader, under one deadline.

    The source and ref are validated and the allowlist judged for ``tenant_id`` before anything
    reaches GitHub; the reader's client is closed on the way out; a deadline overrun is an
    upstream failure with a code, not a timeout the caller has to interpret."""
    from felix.skills.policy import load_publish_policy

    parsed = parse_source(source)
    check_allowed(settings, parsed, tenant_id)
    wanted_ref = validate_ref(ref) if ref else None
    days = (await load_publish_policy(settings, tenant_id)).policy.import_min_age_days
    cooldown = Cooldown(days=days, now=deps.clock())
    try:
        async with reader(settings, deps.http, deps.charge) as gh, asyncio.timeout(DEADLINE_SECONDS):
            yield _Session(parsed, wanted_ref, cooldown, gh)
    except TimeoutError:
        raise ImportUpstreamError(f"GitHub took longer than {DEADLINE_SECONDS:.0f}s") from None


# -- sanitising --------------------------------------------------------------------------------


def keeps_path(path: str) -> bool:
    """Whether an import keeps a file: `SKILL.md`, and any path the bundle format accepts
    (`format.bundle_path_issue`) but `evals/` and dot-paths -- a binary asset only under
    `assets/`, the one place the format takes one."""
    if path == "SKILL.md":
        return True
    if bundle_path_issue(path) is not None:
        return False
    segments = path.split("/")
    if path not in ALLOWED_ROOT_FILES and segments[0] not in KEPT_DIRS:
        return False
    if any(s.startswith(".") for s in segments):
        return False
    return not is_binary_asset_path(path) or path.startswith("assets/")


def sanitize_bundle(raw: Mapping[str, bytes]) -> tuple[dict[str, str], list[str]]:
    """The bundle an import saves, and the paths it dropped.

    Kept paths per `keeps_path`; a binary asset becomes base64, as `binary.py` carries it; a text
    file that is not UTF-8 is dropped; the SKILL.md description is clamped.
    """
    files: dict[str, str] = {}
    dropped: list[str] = []
    for path, data in sorted(raw.items()):
        if not keeps_path(path):
            dropped.append(path)
        elif is_binary_asset_path(path):
            files[path] = encode_base64(data)
        else:
            try:
                files[path] = data.decode("utf-8")
            except UnicodeDecodeError:
                dropped.append(path)
    if "SKILL.md" in files:
        files["SKILL.md"] = clamp_description(files["SKILL.md"])
    return files, dropped


def _bundle_size(path: str, size: int) -> int:
    # What `validate_skill_bundle` will count: a binary asset travels as base64 text.
    return base64_encoded_size(size) if is_binary_asset_path(path) else size


def _check_caps(kept: list[TreeEntry], source: str) -> None:
    """Refuse, before any blob is fetched, a skill the bundle format would refuse anyway."""
    if len(kept) > MAX_BUNDLE_FILES:
        raise ImportSourceTooLarge(f"{source} has {len(kept)} files; a skill may hold {MAX_BUNDLE_FILES}")
    for entry in kept:
        if entry.size > MAX_BINARY_ASSET_BYTES:
            raise ImportSourceTooLarge(
                f"{entry.path} is {entry.size} bytes; a file may be {MAX_BINARY_ASSET_BYTES}"
            )
    total = sum(_bundle_size(e.path, e.size) for e in kept)
    if total > MAX_BUNDLE_BYTES:
        raise ImportSourceTooLarge(f"{source} totals {total} bytes; a skill may total {MAX_BUNDLE_BYTES}")


def _frontmatter(skill_md: bytes) -> dict[str, Any]:
    """A SKILL.md's frontmatter as text, as the catalog reads it; empty when it has none."""
    parsed = parse_skill_md(clamp_description(skill_md.decode("utf-8", "replace")), scalars_as_text=True)
    return parsed.frontmatter if parsed is not None and isinstance(parsed.frontmatter, dict) else {}


# -- browse ------------------------------------------------------------------------------------


def _folder_digest(tree: list[TreeEntry], source_path: str) -> str:
    """The digest an import of this folder would record: over the files it keeps."""
    return hash_tree_snapshot(e for e in skill_file_entries(tree, source_path) if keeps_path(e.path))


async def _listing_meta(gh: GitHubReader, source: GitHubSource, entry: TreeEntry) -> tuple[str | None, str]:
    """A SKILL.md's (name, description), from its first `BROWSE_SKILL_MD_BYTES` alone, parsed as
    it arrives so a listing holds two strings per skill rather than every file."""
    meta = _frontmatter(await gh.blob_head(source, entry, BROWSE_SKILL_MD_BYTES))
    name, description = meta.get("name"), meta.get("description")
    return (
        name if isinstance(name, str) else None,
        description[:MAX_DESCRIPTION_CHARS] if isinstance(description, str) else "",
    )


def _listing_item(
    skill: DiscoveredSkill, meta: tuple[str | None, str], source: str, first_seen_at: int, cooldown: Cooldown
) -> dict[str, Any]:
    name, description = meta
    return {
        "name": name or skill.slug,
        "description": description,
        "path": skill.source_path,
        "source": source,
        "first_seen_at": first_seen_at,
        "eligible_at": cooldown.eligible_at(first_seen_at),
        "eligible": not cooldown.days or cooldown.eligible(first_seen_at),
    }


async def browse(
    settings: Settings, tenant_id: str, source: str, ref: str | None = None, *, deps: ImportDeps | None = None
) -> dict[str, Any]:
    """The skills a repository offers at one commit: each one's path, name and description, read
    from the head of its SKILL.md alone. With a path in ``source``, only the skills under it --
    and the path itself counts as a root, so a layout no default root covers can still be listed.
    A folder whose path is not a valid source (`github.valid_source_path`) is not listed.

    Every listed skill's files are recorded as seen (`sighting_store`), so the cooldown can count
    from here; each item says when it is first eligible. At most `MAX_BROWSE_SKILLS` are listed;
    `found` says how many there were."""
    deps = deps or ImportDeps()
    async with _github_session(settings, tenant_id, source, ref, deps) as session:
        parsed, cooldown, gh = session.source, session.cooldown, session.gh
        resolved = await resolve(gh, parsed, session.ref)
        roots = (*DEFAULT_ROOTS, parsed.path) if parsed.path else DEFAULT_ROOTS
        prefix = f"{parsed.path}/"
        found = [
            d
            for d in discover_skills(resolved.tree, roots)
            if (not parsed.path or d.source_path == parsed.path or d.source_path.startswith(prefix))
            and valid_source_path(d.source_path)
        ]
        listed = found[:MAX_BROWSE_SKILLS]
        by_path = {e.path: e for e in resolved.tree}
        metas = await _gather_limited(
            (lambda d=d: _listing_meta(gh, parsed, by_path[d.skill_md_path])) for d in listed
        )
    sources = {d.source_path: parsed.at(d.source_path).canonical for d in listed}
    digests = {d.source_path: _folder_digest(resolved.tree, d.source_path) for d in listed}
    seen = await get_sighting_store(settings).first_seen(
        tenant_id, [(sources[p], digests[p]) for p in sources], at=cooldown.now
    )
    return {
        "source": parsed.canonical,
        "ref": resolved.ref,
        "commit": resolved.commit,
        "license": resolved.license,
        "min_age_days": cooldown.days,
        "items": [
            _listing_item(
                skill,
                meta,
                sources[skill.source_path],
                seen[(sources[skill.source_path], digests[skill.source_path])],
                cooldown,
            )
            for skill, meta in zip(listed, metas, strict=True)
        ],
        "found": len(found),
        "truncated": len(found) > len(listed),
    }


# -- import ------------------------------------------------------------------------------------


@dataclass(slots=True)
class ImportResult:
    version: dict[str, Any]
    unchanged: bool
    dropped_files: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _Fetched:
    resolved: Resolved
    tree_hash: str
    files: dict[str, str]
    dropped: list[str]
    parent: str | None
    committed_at: int | None


def _slug(source: GitHubSource) -> str:
    """The skill's folder name, which its SKILL.md must name: the last path segment, or the
    repository for a skill at its root."""
    return source.path.rsplit("/", 1)[-1] if source.path else source.repo


async def _prior(
    settings: Settings, tenant_id: str, name: str, origin: str, tree_hash: str
) -> tuple[dict[str, Any] | None, str | None]:
    """(the version a re-import would not change; else None) and the version a new one builds on
    (None for a skill the library does not hold).

    Judged against the newest version that was not rejected: a rejected draft is not what the
    import replaces. Refuses a name whose newest such version came from anywhere but ``origin``:
    an import never takes over a skill an agent or an operator wrote, or one imported from
    somewhere else."""
    lib = get_skill_library_store(settings)
    buildable = (await lib.buildable_versions(tenant_id, [name])).get(name, [])
    newest = library.newest_version(buildable)
    if newest is None:
        return None, None
    row = await lib.get_version(tenant_id, name, newest) or {}
    if row.get("source") != "import" or row.get("origin_source") != origin:
        held = row.get("origin_source") if row.get("source") == "import" else f"an {row.get('source')}"
        raise library.SkillOriginMismatch(
            f"{name}@{newest} came from {held}, not {origin}; an import never replaces another origin's skill"
        )
    if row.get("origin_tree_hash") == tree_hash:
        return {**row, "tenant_id": tenant_id}, newest
    return None, newest


async def _fetch(settings: Settings, tenant_id: str, session: _Session) -> _Fetched | ImportResult:
    """Resolve and list the skill folder, record the sighting, and download it -- unless it is
    inside the cooldown (refused before any file is read), or the library already holds exactly
    these files, which the digest alone shows."""
    source, gh, cooldown = session.source, session.gh, session.cooldown
    resolved = await resolve(gh, source, session.ref)
    entries = skill_file_entries(resolved.tree, source.path)
    if not any(e.path == "SKILL.md" for e in entries):
        raise ImportSourceNotFound(f"{source.canonical} holds no SKILL.md at {resolved.commit}")
    kept = [e for e in entries if keeps_path(e.path)]
    tree_hash = hash_tree_snapshot(kept)
    # Recorded before it is judged, whatever the cooldown: a refused attempt still starts the clock.
    first_seen = (
        await get_sighting_store(settings).first_seen(
            tenant_id, [(source.canonical, tree_hash)], at=cooldown.now
        )
    )[(source.canonical, tree_hash)]
    cooldown.check(source.canonical, first_seen)
    _check_caps(kept, source.canonical)
    unchanged, parent = await _prior(settings, tenant_id, _slug(source), source.canonical, tree_hash)
    if unchanged is not None:
        return ImportResult(version=unchanged, unchanged=True)
    bodies = await _gather_limited((lambda e=e: gh.blob(source, e)) for e in kept)
    files, dropped = sanitize_bundle(dict(zip((e.path for e in kept), bodies, strict=True)))
    dropped = sorted({*dropped, *(e.path for e in entries if not keeps_path(e.path))})
    return _Fetched(
        resolved=resolved,
        tree_hash=tree_hash,
        files=files,
        dropped=dropped,
        parent=parent,
        # Provenance only: the pusher sets it, so nothing decides on it.
        committed_at=await gh.last_changed(source, resolved.commit, source.path),
    )


async def import_skill(
    settings: Settings,
    tenant_id: str,
    *,
    source: str,
    ref: str | None = None,
    by: str,
    deps: ImportDeps | None = None,
) -> ImportResult:
    """Fetch the skill at ``source`` (pinned to the commit ``ref`` resolves to) and save it as a
    draft by ``by``, or return the newest version unchanged when its files are the same.

    Refused (`too_recent`) while these files were first seen by this tenant within its minimum
    import age, judged at ``deps.clock()``."""
    deps = deps or ImportDeps()
    async with _github_session(settings, tenant_id, source, ref, deps) as session:
        fetched = await _fetch(settings, tenant_id, session)
    if isinstance(fetched, ImportResult):
        return fetched
    parsed, resolved = session.source, fetched.resolved
    saved = await library.save_draft(
        settings,
        tenant_id,
        files=fetched.files,
        # The folder's name: a SKILL.md naming another skill is refused, not filed under its claim.
        name=_slug(parsed),
        provenance=library.DraftProvenance(
            source="import",
            author=by,
            reason=f"imported from {parsed.canonical}@{resolved.commit}",
            principal=by,
            origin=library.ImportOrigin(
                source=parsed.canonical,
                ref=resolved.ref,
                commit=resolved.commit,
                tree_hash=fetched.tree_hash,
                license=resolved.license,
                committed_at=fetched.committed_at,
            ),
        ),
        parent=fetched.parent,
        # Optimistic concurrency against the version the origin check read: of two imports racing
        # to the same skill, one saves and the other is refused rather than stacked on it.
        expect_newest=fetched.parent if fetched.parent is not None else library.MUST_NOT_EXIST,
        object_store=deps.object_store,
    )
    logger.info(
        "skill imported skill=%s version=%s source=%s commit=%s dropped=%d",
        saved["name"],
        saved["version"],
        parsed.canonical,
        resolved.commit,
        len(fetched.dropped),
    )
    return ImportResult(version=saved, unchanged=False, dropped_files=fetched.dropped)


__all__ = [
    "BROWSE_SKILL_MD_BYTES",
    "DAY_MS",
    "DEADLINE_SECONDS",
    "KEPT_DIRS",
    "MAX_BROWSE_SKILLS",
    "Cooldown",
    "ImportDeps",
    "ImportResult",
    "browse",
    "github_call_budget",
    "import_skill",
    "keeps_path",
    "sanitize_bundle",
]
