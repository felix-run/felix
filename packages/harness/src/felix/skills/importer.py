"""Import Agent Skills from GitHub into the tenant library, as drafts, and browse what a
repository offers.

The GitHub half -- source syntax, the allowlist, the pinned fetch -- is `skills/github.py`. This
is what the library makes of it.

What lands is a draft (`library.save_draft`, `source="import"`), never a live skill: publishing
is a separate, explicit step through the same gate an operator's upload passes, and an imported
version is held to a stricter one (`publish_gate.policy_for_source`: an advisory scan blocks).

The bundle is sanitised before it is validated, as Skillist's mirror does: `SKILL.md`,
`plugin.json`, `scripts/`, `references/` and `assets/` are kept and everything else (examples,
tests, a LICENSE file, dot-paths) is dropped and reported, and an over-long description is clamped.

Change detection is a digest of the skill folder's tree entries (`github.hash_tree_snapshot`),
not the commit SHA: a repository commits constantly, and an unrelated commit must not mint a new
version. A re-import whose digest matches the newest version's saves nothing (`unchanged`).

An import never takes over a name: if the library holds the skill from another source, or an
agent or an operator wrote its newest version, the import is refused (`origin_mismatch`).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from felix.config import Settings
from felix.skills import library
from felix.skills.binary import MAX_BINARY_ASSET_BYTES, encode_base64, is_binary_asset_path
from felix.skills.format import (
    MAX_BUNDLE_BYTES,
    MAX_BUNDLE_FILES,
    MAX_SKILL_MD_CHARS,
    SKILL_NAME_RE,
    parse_skill_md,
)
from felix.skills.github import (
    DEFAULT_ROOTS,
    GitHubReader,
    GitHubSource,
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
    validate_ref,
)
from felix.skills.library_store import get_skill_library_store

logger = logging.getLogger("felix.skills.importer")

# What an import keeps: the agentskills.io layout less `evals/`. A bundle's evaluation scenarios
# count toward the publish gate only when an operator wrote them (`publish_gate.eval_counts_for_gate`),
# so a third party's are dropped rather than carried as if they were.
KEPT_ROOT_FILES = frozenset({"SKILL.md", "plugin.json"})
KEPT_DIRS = frozenset({"scripts", "references", "assets"})
# agentskills.io's limit; a longer description is clamped rather than refused, as Skillist does.
MAX_DESCRIPTION_CHARS = 1024
# Skills one browse reads a SKILL.md for.
MAX_BROWSE_SKILLS = 200
# Wall clock for one whole import or browse, every GitHub call included.
DEADLINE_SECONDS = 120.0
_FETCH_CONCURRENCY = 8
DAY_MS = 86_400_000

now_ms = lambda: int(time.time() * 1000)

_FRONTMATTER_RE = re.compile(r"^---\r?\n([\s\S]*?)\r?\n---(\r?\n[\s\S]*)\Z")
_DESCRIPTION_RE = re.compile(r"^description:[ \t]*(.*)$", re.MULTILINE)


def _checked_request(settings: Settings, source: str, ref: str | None) -> tuple[GitHubSource, str | None]:
    parsed = parse_source(source)
    check_allowed(settings, parsed)
    return parsed, validate_ref(ref) if ref else None


async def _gather_limited[T](calls: Iterable[Awaitable[T]]) -> list[T]:
    gate = asyncio.Semaphore(_FETCH_CONCURRENCY)

    async def one(call: Awaitable[T]) -> T:
        async with gate:
            return await call

    return await asyncio.gather(*(one(c) for c in calls))


# -- the cooldown --------------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class Cooldown:
    """The minimum import age in force for a tenant (`FELIX_SKILL_IMPORT_MIN_AGE_DAYS`, tightened by
    the tenant's policy row), and the moment it is judged at."""

    days: int
    now: int

    def eligible_at(self, committed_at: int) -> int:
        return committed_at + self.days * DAY_MS

    def check(self, what: str, committed_at: int) -> None:
        """Refuse ``what`` when its folder changed less than ``days`` ago. At exactly the boundary
        it is old enough. A hard refusal: nothing is saved, and no flag overrides it."""
        if self.days and self.now < self.eligible_at(committed_at):
            raise ImportTooRecent(
                f"{what} last changed {_iso(committed_at)}; the minimum import age is {self.days} days, "
                f"so it can be imported from {_iso(self.eligible_at(committed_at))}"
            )


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat(timespec="seconds")


async def _cooldown(settings: Settings, tenant_id: str, clock: Callable[[], int]) -> Cooldown:
    """The tenant's minimum import age: the policy row tightens the setting, never loosens it."""
    from felix.skills.policy import load_publish_policy

    return Cooldown(
        days=(await load_publish_policy(settings, tenant_id)).policy.import_min_age_days, now=clock()
    )


# -- sanitising --------------------------------------------------------------------------------


def keeps_path(path: str) -> bool:
    """Whether an import keeps a file: `SKILL.md`, `plugin.json`, and anything under `scripts/`,
    `references/` or `assets/` that is not a dot-path. A binary asset only under `assets/`, which
    is the only place the bundle format accepts one."""
    if path in KEPT_ROOT_FILES:
        return True
    segments = path.split("/")
    if len(segments) < 2 or segments[0] not in KEPT_DIRS or any(s.startswith(".") for s in segments):
        return False
    return segments[0] == "assets" or not is_binary_asset_path(path)


def truncate_frontmatter_description(skill_md: str, max_len: int = MAX_DESCRIPTION_CHARS) -> str:
    """Clamp a one-line `description:` to ``max_len`` characters without re-serialising the
    frontmatter, so nothing else in the file changes."""
    match = _FRONTMATTER_RE.match(skill_md)
    if not match:
        return skill_md
    frontmatter, body = match.group(1), match.group(2)
    found = _DESCRIPTION_RE.search(frontmatter)
    if not found:
        return skill_md
    value = found.group(1)
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        quote_char, inner = value[0], value[1:-1]
        if len(inner) <= max_len:
            return skill_md
        value = f"{quote_char}{inner[: max_len - 1]}…{quote_char}"
    elif len(value) > max_len:
        value = f"{value[: max_len - 1]}…"
    else:
        return skill_md
    frontmatter = f"{frontmatter[: found.start()]}description: {value}{frontmatter[found.end() :]}"
    return f"---\n{frontmatter}\n---{body}"


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
        files["SKILL.md"] = truncate_frontmatter_description(files["SKILL.md"])
    return files, dropped


def _bundle_size(path: str, size: int) -> int:
    # What `validate_skill_bundle` will count: a binary asset travels as base64 text.
    return (size + 2) // 3 * 4 if is_binary_asset_path(path) else size


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
    parsed = parse_skill_md(
        truncate_frontmatter_description(skill_md.decode("utf-8", "replace")), scalars_as_text=True
    )
    return parsed.frontmatter if parsed is not None and isinstance(parsed.frontmatter, dict) else {}


# -- browse ------------------------------------------------------------------------------------


async def _listing_body(gh: GitHubReader, source: GitHubSource, entry: TreeEntry) -> bytes:
    # A SKILL.md past the bundle format's own limit lists with no description rather than
    # costing a download nothing could save.
    return b"" if entry.size > MAX_SKILL_MD_CHARS * 4 else await gh.blob(source, entry)


def _listing_item(
    parsed: GitHubSource, skill: Any, body: bytes, committed_at: int | None, cooldown: Cooldown
) -> dict[str, Any]:
    meta = _frontmatter(body)
    name, description = meta.get("name"), meta.get("description")
    eligible_at = cooldown.eligible_at(committed_at) if committed_at is not None else None
    return {
        "name": name if isinstance(name, str) else skill.slug,
        "description": description[:MAX_DESCRIPTION_CHARS] if isinstance(description, str) else "",
        "path": skill.source_path,
        "source": parsed.at(skill.source_path).canonical,
        "committed_at": committed_at,
        "eligible_at": eligible_at,
        "eligible": eligible_at is None or cooldown.now >= eligible_at,
    }


async def browse(
    settings: Settings,
    tenant_id: str,
    source: str,
    ref: str | None = None,
    *,
    http: httpx.AsyncClient | None = None,
    clock: Callable[[], int] = now_ms,
) -> dict[str, Any]:
    """The skills a repository offers at one commit: each one's path, name and description, read
    from its SKILL.md alone. With a path in ``source``, only the skills under it -- and the path
    itself counts as a root, so a layout no default root covers can still be listed.

    Under a minimum import age, each listed skill is dated (one more GitHub call per skill, so
    only then) and says when it becomes eligible; with no cooldown nothing is dated and every
    skill is eligible."""
    parsed, wanted_ref = _checked_request(settings, source, ref)
    cooldown = await _cooldown(settings, tenant_id, clock)
    prefix = f"{parsed.path}/"
    try:
        async with reader(settings, http) as gh, asyncio.timeout(DEADLINE_SECONDS):
            resolved = await resolve(gh, parsed, wanted_ref)
            roots = (*DEFAULT_ROOTS, parsed.path) if parsed.path else DEFAULT_ROOTS
            found = [
                d
                for d in discover_skills(resolved.tree, roots)
                if not parsed.path or d.source_path == parsed.path or d.source_path.startswith(prefix)
            ]
            listed = found[:MAX_BROWSE_SKILLS]
            by_path = {e.path: e for e in resolved.tree}
            bodies = await _gather_limited(
                _listing_body(gh, parsed, by_path[d.skill_md_path]) for d in listed
            )
            dates: list[int | None] = (
                list(
                    await _gather_limited(
                        gh.last_changed(parsed, resolved.commit, d.source_path) for d in listed
                    )
                )
                if cooldown.days
                else [None] * len(listed)
            )
    except TimeoutError:
        raise ImportUpstreamError(f"GitHub took longer than {DEADLINE_SECONDS:.0f}s") from None
    return {
        "source": parsed.canonical,
        "ref": resolved.ref,
        "commit": resolved.commit,
        "license": resolved.license,
        "min_age_days": cooldown.days,
        "items": [
            _listing_item(parsed, skill, body, at, cooldown)
            for skill, body, at in zip(listed, bodies, dates, strict=True)
        ],
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
    committed_at: int


async def _prior(
    settings: Settings, tenant_id: str, name: Any, origin: str, tree_hash: str
) -> tuple[dict[str, Any] | None, str | None]:
    """(the newest version, when a re-import would change nothing; else None) and the version a
    new one builds on (None for a skill the library does not hold, or a name that is not one).

    Refuses a name the library holds from anywhere but ``origin``: an import never takes over a
    skill an agent or an operator wrote, or one imported from somewhere else."""
    if not isinstance(name, str) or not SKILL_NAME_RE.match(name):
        return None, None  # the save's validation says what is wrong with it
    lib = get_skill_library_store(settings)
    newest = library.newest_version(await lib.version_ids(tenant_id, name))
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


async def _fetch(
    settings: Settings,
    tenant_id: str,
    gh: GitHubReader,
    source: GitHubSource,
    ref: str | None,
    cooldown: Cooldown,
) -> _Fetched | ImportResult:
    """Resolve, date and list the skill folder, and download it -- unless it is too recent to
    import (refused before any file is read), or the library already holds exactly this tree,
    which needs only the SKILL.md (for its name) to find out."""
    resolved = await resolve(gh, source, ref)
    entries = skill_file_entries(resolved.tree, source.path)
    skill_md_entry = next((e for e in entries if e.path == "SKILL.md"), None)
    if skill_md_entry is None:
        raise ImportSourceNotFound(f"{source.canonical} holds no SKILL.md at {resolved.commit[:12]}")
    # The folder's own last change at the commit, not the repository HEAD's.
    committed_at = await gh.last_changed(source, resolved.commit, source.path)
    cooldown.check(source.canonical, committed_at)
    tree_hash = hash_tree_snapshot(entries)
    kept = [e for e in entries if keeps_path(e.path)]
    _check_caps(kept, source.canonical)
    skill_md = await gh.blob(source, skill_md_entry)
    name = _frontmatter(skill_md).get("name")
    unchanged, parent = await _prior(settings, tenant_id, name, source.canonical, tree_hash)
    if unchanged is not None:
        return ImportResult(version=unchanged, unchanged=True)
    rest = [e for e in kept if e.path != "SKILL.md"]
    bodies = await _gather_limited(gh.blob(source, e) for e in rest)
    files, dropped = sanitize_bundle(
        {"SKILL.md": skill_md, **dict(zip((e.path for e in rest), bodies, strict=True))}
    )
    dropped = sorted({*dropped, *(e.path for e in entries if not keeps_path(e.path))})
    return _Fetched(
        resolved=resolved,
        tree_hash=tree_hash,
        files=files,
        dropped=dropped,
        parent=parent,
        committed_at=committed_at,
    )


async def import_skill(
    settings: Settings,
    tenant_id: str,
    *,
    source: str,
    ref: str | None = None,
    by: str,
    object_store: Any | None = None,
    http: httpx.AsyncClient | None = None,
    clock: Callable[[], int] = now_ms,
) -> ImportResult:
    """Fetch the skill at ``source`` (pinned to the commit ``ref`` resolves to) and save it as a
    draft by ``by``, or return the newest version unchanged when its files are the same.

    Refused (`too_recent`) when the skill's folder changed within the tenant's minimum import
    age, judged at ``clock()``. ``http`` is a client to reach GitHub with, left open; None is the
    egress-pinned production one (`github.github_client`)."""
    parsed, wanted_ref = _checked_request(settings, source, ref)
    cooldown = await _cooldown(settings, tenant_id, clock)
    try:
        async with reader(settings, http) as gh, asyncio.timeout(DEADLINE_SECONDS):
            fetched = await _fetch(settings, tenant_id, gh, parsed, wanted_ref, cooldown)
    except TimeoutError:
        raise ImportUpstreamError(f"GitHub took longer than {DEADLINE_SECONDS:.0f}s") from None
    if isinstance(fetched, ImportResult):
        return fetched
    resolved = fetched.resolved
    saved = await library.save_draft(
        settings,
        tenant_id,
        files=fetched.files,
        provenance=library.DraftProvenance(
            source="import",
            author=by,
            reason=f"imported from {parsed.canonical}@{resolved.commit[:12]}",
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
        # Optimistic concurrency against the version the origin check read.
        expect_newest=fetched.parent if fetched.parent is not None else library.MUST_NOT_EXIST,
        object_store=object_store,
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
    "DAY_MS",
    "DEADLINE_SECONDS",
    "KEPT_DIRS",
    "KEPT_ROOT_FILES",
    "MAX_BROWSE_SKILLS",
    "MAX_DESCRIPTION_CHARS",
    "Cooldown",
    "ImportResult",
    "browse",
    "import_skill",
    "keeps_path",
    "sanitize_bundle",
    "truncate_frontmatter_description",
]
