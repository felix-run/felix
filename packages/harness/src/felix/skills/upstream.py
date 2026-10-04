"""Imported skills against their origin: whether an update is waiting, what it changes, and
taking it.

An imported skill (`importer.py`) remembers where it came from on its newest version: the
canonical source, the ref asked for, the commit and the kept-file digest. Three things are built
on that, none of which a manifest compile ever reaches -- GitHub is asked here, on a request or on
the worker's sweep, and never while an agent is being built:

- **A check** (`check_upstream`) re-resolves the stored ref (or a ref the caller names, under the
  same allowlist), digests the kept files there, and diffs them against the version that is live --
  the newest when nothing is. Only changed files are fetched: a file's git blob id is computed
  from the stored bytes and compared with the tree's, and only a text file whose id differs is
  read; a binary one is compared by id and reported by size. The check stamps a sighting, as a
  browse does: asking about an update starts its cooldown clock.
- **An update** (`update_skill`) is `importer.import_skill` from the stored origin, with the same
  outcomes -- a new draft, `unchanged`, `too_recent`, `origin_mismatch` -- and never a publish.
  Its diff is between two stored versions and costs no GitHub call.
- **The listing** (`outdated`) and **the sweep** (`run_upstream_checks`) check without diffs,
  and record what they found (`upstream_store`) so the listing can answer from the record, and
  the library detail always does.

Every GitHub call is charged to the tenant's and the deployment's hourly budget
(`importer.github_call_budget`); the sweep charges a share of each, so it stops before people
asking would be refused. Upstream text is untrusted: the routes redact it as a file read is, and
the CLI strips control characters from it.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from felix.config import Settings
from felix.skills import importer, library
from felix.skills.binary import decode_base64, is_binary_asset_path
from felix.skills.bundle_diff import Content, DiffBuilder, diff_bundles, git_blob_id
from felix.skills.github import (
    ImportBudgetExhausted,
    Resolved,
    SkillImportError,
    SkillNotImported,
    TreeEntry,
    resolve,
)
from felix.skills.library_store import get_skill_library_store
from felix.skills.upstream_store import get_upstream_store

logger = logging.getLogger("felix.skills.upstream")

# Imported skills one listing checks: each is three to five GitHub calls (fewer when several come
# from one repository and ref, which are resolved once).
MAX_OUTDATED = 25
# No check of a listing starts this long after its first: each has its own deadline
# (`importer.DEADLINE_SECONDS`), and the whole listing must answer before a client gives up.
LISTING_SECONDS = 30.0
# Skills the sweep checks per tick, oldest check first, across every tenant.
SWEEP_BATCH = 50
# The fraction of each hourly budget the sweep may spend: the rest is for people asking.
SWEEP_SHARE = 0.5
# The sweep's `skill_job_lease` row, and how long it holds between renewals: one check's deadline
# and a margin. Renewed before every check.
SWEEP_LEASE = "skill_upstream"
SWEEP_LEASE_MS = int(importer.DEADLINE_SECONDS + 60) * 1000
HOUR_MS = 3_600_000
_LIBRARY_PAGE = 100
_FETCH_BATCH = 8


# -- diffs ---------------------------------------------------------------------------------------


async def _stored_files(
    settings: Settings, tenant_id: str, name: str, version: str | None, object_store: Any | None
) -> dict[str, bytes]:
    """Every file of a saved version as stored bytes, each checked against its recorded digest
    (`library.read_version_files`); nothing for no version."""
    if version is None:
        return {}
    files = await library.read_version_files(settings, tenant_id, name, version, object_store=object_store)
    return {p: decode_base64(t) if is_binary_asset_path(p) else t.encode("utf-8") for p, t in files.items()}


async def diff_versions(
    settings: Settings,
    tenant_id: str,
    name: str,
    old_version: str | None,
    new_version: str,
    *,
    object_store: Any | None = None,
) -> dict[str, Any]:
    """The files that differ between two saved versions, from the stores alone."""
    old = await _stored_files(settings, tenant_id, name, old_version, object_store)
    new = await _stored_files(settings, tenant_id, name, new_version, object_store)
    return {"compared_with": old_version, **diff_bundles(old, new)}


async def _diff_upstream(
    session: Any, kept: list[TreeEntry], base: Mapping[str, bytes], base_version: str
) -> dict[str, Any]:
    """The kept files at the resolved commit against the stored ``base``, fetching only what the
    tree's blob ids say changed -- and of that, only text, in path order, until the diff budget is
    spent; a binary asset is compared by id and reported by size.

    A fetched file is sanitised as an import would save it (`importer.sanitize_bundle`): a
    SKILL.md whose description an import would clamp is compared clamped, and text that is not
    UTF-8, which an import drops, counts as absent."""
    upstream = {e.path: e for e in kept}
    builder = DiffBuilder()
    to_read: list[TreeEntry] = []
    for path in sorted(base.keys() | upstream.keys()):
        stored, entry = base.get(path), upstream.get(path)
        old = Content.of(path, stored) if stored is not None else None
        if entry is None:
            builder.add(path, old, None)
        elif stored is not None and git_blob_id(stored, entry.sha) == entry.sha:
            continue
        elif is_binary_asset_path(path):
            builder.add(path, old, Content(entry.size))
        else:
            to_read.append(entry)
    for at in range(0, len(to_read), _FETCH_BATCH):
        batch = to_read[at : at + _FETCH_BATCH]
        texts = await _read_text(session, batch) if not builder.exhausted else {}
        for entry in batch:
            stored = base.get(entry.path)
            old = Content.of(entry.path, stored) if stored is not None else None
            if builder.exhausted and entry.path not in texts:
                builder.add(entry.path, old, Content(entry.size))
                continue
            data = texts.get(entry.path)
            if data != stored:
                builder.add(entry.path, old, Content.of(entry.path, data) if data is not None else None)
    return {"compared_with": base_version, **builder.result()}


async def _read_text(session: Any, batch: list[TreeEntry]) -> dict[str, bytes]:
    """The text files of ``batch`` as an import would store them; one that is not UTF-8 is absent."""
    bodies = await importer._gather_limited((lambda e=e: session.gh.blob(session.source, e)) for e in batch)
    texts, _ = importer.sanitize_bundle(dict(zip((e.path for e in batch), bodies, strict=True)))
    return {path: text.encode("utf-8") for path, text in texts.items()}


# -- what the library holds ----------------------------------------------------------------------


async def imported_head(
    settings: Settings, tenant_id: str, name: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The skill and its newest version that was not rejected, which must be an import: it names
    the origin a check or an update reads. `SkillNotFound` for a skill the library does not hold,
    `SkillNotImported` (409 `not_imported`) for one whose newest version came from anywhere else."""
    lib = get_skill_library_store(settings)
    skill = await lib.get_skill(tenant_id, name)
    if skill is None:
        raise library.SkillNotFound(f"{name} is not in the library")
    newest = library.newest_version((await lib.buildable_versions(tenant_id, [name])).get(name, []))
    head = await lib.get_version(tenant_id, name, newest) if newest is not None else None
    if head is None or head.get("source") != "import" or not head.get("origin_source"):
        raise SkillNotImported(f"{name} was not imported, so it has no origin to check")
    return skill, head


async def _imported_page(
    settings: Settings, tenant_id: str, after: str | None, limit: int
) -> tuple[list[tuple[dict[str, Any], dict[str, Any]]], str | None]:
    """Up to ``limit`` of the tenant's imported skills after ``after``, by name, each with its
    newest version that was not rejected; and the cursor past the last, or None at the end."""
    lib = get_skill_library_store(settings)
    found: list[tuple[dict[str, Any], dict[str, Any]]] = []
    cursor = after
    while True:
        page = await lib.list_skills(tenant_id, limit=_LIBRARY_PAGE, after=cursor)
        buildable = await lib.buildable_versions(tenant_id, [s["name"] for s in page])
        for skill in page:
            newest = library.newest_version(buildable.get(skill["name"], []))
            head = await lib.get_version(tenant_id, skill["name"], newest) if newest is not None else None
            if head is not None and head.get("source") == "import" and head.get("origin_source"):
                found.append((skill, head))
                if len(found) == limit:
                    return found, skill["name"]
        if len(page) < _LIBRARY_PAGE:
            return found, None
        cursor = page[-1]["name"]


def _current(skill: Mapping[str, Any], head: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "version": head["version"],
        "status": head["status"],
        "source": head["origin_source"],
        "ref": head["origin_ref"],
        "commit": head["origin_commit"],
        "tree_hash": head["origin_tree_hash"],
        "live_version": skill.get("live_version"),
    }


def describe(
    head: Mapping[str, Any], state: Mapping[str, Any] | None, *, min_age_days: int, now: int
) -> dict[str, Any]:
    """What the last recorded check of ``head``'s origin found: the upstream commit and digest,
    whether that is an update (a digest other than the newest version's), and when the tenant's
    cooldown lets it in. Fields the record does not hold are null."""
    state = state or {}
    tree_hash, first_seen = state.get("upstream_tree_hash"), state.get("first_seen_at")
    eligible_at = first_seen + min_age_days * importer.DAY_MS if first_seen is not None else None
    return {
        "upstream_commit": state.get("upstream_commit"),
        "upstream_tree_hash": tree_hash,
        "update_available": tree_hash is not None and tree_hash != head.get("origin_tree_hash"),
        "first_seen_at": first_seen,
        "eligible_at": eligible_at,
        "eligible": eligible_at is not None and (not min_age_days or now >= eligible_at),
        "checked_at": state.get("checked_at"),
        "error": state.get("error"),
    }


# -- the check -----------------------------------------------------------------------------------


async def check_upstream(
    settings: Settings,
    tenant_id: str,
    name: str,
    *,
    ref: str | None = None,
    deps: importer.ImportDeps | None = None,
) -> dict[str, Any]:
    """The skill's origin now against the library: the upstream commit and digest, when the
    tenant's cooldown lets it in, whether it is an update, and a per-file diff against the live
    version (the newest when nothing is live).

    ``ref`` names another branch, tag or commit of the same source than the one stored, under the
    same allowlist and the same rules as an import's ref. A check of the stored ref is recorded
    (`upstream_store`); one of another ref is a what-if, and is not."""
    deps = deps or importer.ImportDeps()
    skill, head = await imported_head(settings, tenant_id, name)
    other_ref = ref if ref and ref != head["origin_ref"] else None
    base_version = skill.get("live_version") or head["version"]
    base = await _stored_files(settings, tenant_id, name, base_version, deps.object_store)
    async with importer._github_session(
        settings, tenant_id, head["origin_source"], other_ref or head["origin_ref"], deps
    ) as session:
        snap = await importer.snapshot(settings, tenant_id, session)
        importer._check_caps(snap.kept, session.source.canonical)
        committed_at = await session.gh.last_changed(
            session.source, snap.resolved.commit, session.source.path
        )
        diff = await _diff_upstream(session, snap.kept, base, base_version)
    cooldown = session.cooldown
    if other_ref is None:
        await importer._record(settings, tenant_id, session.source, snap, cooldown.now)
    eligible_at = cooldown.eligible_at(snap.first_seen)
    return {
        "name": name,
        "current": _current(skill, head),
        "upstream": {
            "source": session.source.canonical,
            "ref": snap.resolved.ref,
            "commit": snap.resolved.commit,
            "tree_hash": snap.tree_hash,
            "license": snap.resolved.license,
            "committed_at": committed_at,
            "first_seen_at": snap.first_seen,
            "eligible_at": eligible_at,
            "eligible": not cooldown.days or cooldown.eligible(snap.first_seen),
        },
        "update_available": snap.tree_hash != head.get("origin_tree_hash"),
        "min_age_days": cooldown.days,
        "diff": diff,
    }


async def update_skill(
    settings: Settings,
    tenant_id: str,
    name: str,
    *,
    by: str,
    ref: str | None = None,
    deps: importer.ImportDeps | None = None,
) -> tuple[importer.ImportResult, dict[str, Any]]:
    """Re-import the skill from its stored origin (``ref`` instead of the stored one, if given),
    exactly as `importer.import_skill` would -- a new draft or `unchanged`, never a publish --
    and the diff between what was live (else the version the draft was built on) and the result."""
    deps = deps or importer.ImportDeps()
    skill, head = await imported_head(settings, tenant_id, name)
    result = await importer.import_skill(
        settings,
        tenant_id,
        source=head["origin_source"],
        ref=ref or head["origin_ref"],
        by=by,
        deps=deps,
        action="update",
    )
    version = str(result.version["version"])
    base = skill.get("live_version") or (version if result.unchanged else result.parent)
    diff = await diff_versions(settings, tenant_id, name, base, version, object_store=deps.object_store)
    return result, diff


async def _check_one(
    settings: Settings,
    tenant_id: str,
    head: Mapping[str, Any],
    deps: importer.ImportDeps,
    resolved: dict[tuple[str, str, str | None], Resolved],
) -> dict[str, Any]:
    """Check one imported skill's stored origin without a diff, and record it. ``resolved`` is
    shared by the checks of one listing: skills from one repository and ref resolve once."""
    async with importer._github_session(
        settings, tenant_id, head["origin_source"], head["origin_ref"], deps
    ) as session:
        key = (session.source.owner, session.source.repo, session.ref)
        if key not in resolved:
            resolved[key] = await resolve(session.gh, session.source, session.ref)
        snap = await importer.snapshot(settings, tenant_id, session, resolved[key])
    await importer._record(settings, tenant_id, session.source, snap, session.cooldown.now)
    return {
        "upstream_commit": snap.resolved.commit,
        "upstream_tree_hash": snap.tree_hash,
        "first_seen_at": snap.first_seen,
        "checked_at": session.cooldown.now,
        "error": None,
    }


async def _record_failure(
    settings: Settings, tenant_id: str, head: Mapping[str, Any], exc: SkillImportError, now: int
) -> dict[str, Any]:
    """A failed check: when, and the refusal's code. The last good upstream state is kept."""
    state = {"checked_at": now, "error": exc.code}
    store = get_upstream_store(settings)
    await store.record(
        tenant_id,
        str(head["name"]),
        {"origin_source": head["origin_source"], "origin_ref": head["origin_ref"], **state},
    )
    return (await store.get(tenant_id, [str(head["name"])])).get(str(head["name"]), state)


async def _check_page(
    settings: Settings,
    tenant_id: str,
    heads: list[tuple[dict[str, Any], dict[str, Any]]],
    deps: importer.ImportDeps,
) -> tuple[list[Mapping[str, Any] | None], str | None]:
    """Check each of ``heads`` in turn, recording each: the states, and why it stopped early
    (`rate_limited`, `deadline`) or None. A spent budget before the first check is raised."""
    resolved: dict[tuple[str, str, str | None], Resolved] = {}
    states: list[Mapping[str, Any] | None] = []
    started = time.monotonic()
    for at, (_, head) in enumerate(heads):
        if at and time.monotonic() - started > LISTING_SECONDS:
            return states, "deadline"
        try:
            states.append(await _check_one(settings, tenant_id, head, deps, resolved))
        except ImportBudgetExhausted:
            if at == 0:
                raise
            return states, "rate_limited"
        except SkillImportError as exc:
            states.append(await _record_failure(settings, tenant_id, head, exc, deps.clock()))
    return states, None


async def outdated(
    settings: Settings,
    tenant_id: str,
    *,
    after: str | None = None,
    limit: int = MAX_OUTDATED,
    refresh: bool = True,
    deps: importer.ImportDeps | None = None,
) -> dict[str, Any]:
    """The tenant's imported skills after ``after`` (at most ``limit``), each against its origin:
    checked now (``refresh``), each GitHub call charged; or as last recorded, with no call at all.

    A check that is refused is listed with its code (`error`) and the last good state. A spent
    budget ends the listing there -- `stopped: rate_limited`, with the cursor at the first skill
    not checked -- or, before any skill was, refuses it (`rate_limited`). So does running long:
    no check starts `LISTING_SECONDS` after the first (`stopped: deadline`)."""
    from felix.skills.policy import load_publish_policy

    deps = deps or importer.ImportDeps()
    heads, next_cursor = await _imported_page(settings, tenant_id, after, max(1, min(limit, MAX_OUTDATED)))
    days = (await load_publish_policy(settings, tenant_id)).policy.import_min_age_days
    recorded = await get_upstream_store(settings).get(tenant_id, [str(h["name"]) for _, h in heads])
    stopped: str | None = None
    if refresh:
        states, stopped = await _check_page(settings, tenant_id, heads, deps)
        if stopped is not None:
            heads, next_cursor = heads[: len(states)], str(heads[len(states) - 1][1]["name"])
    else:
        states = [recorded.get(str(h["name"])) for _, h in heads]
    now = deps.clock()
    items = [
        {
            "name": head["name"],
            "version": head["version"],
            "live_version": skill.get("live_version"),
            "origin_source": head["origin_source"],
            "origin_ref": head["origin_ref"],
            "origin_commit": head["origin_commit"],
            **describe(head, state, min_age_days=days, now=now),
        }
        for (skill, head), state in zip(heads, states, strict=True)
    ]
    return {
        "items": items,
        "next_cursor": next_cursor,
        "refreshed": refresh,
        "min_age_days": days,
        "stopped": stopped,
    }


# -- the sweep -----------------------------------------------------------------------------------

_sweep_limiter: Any | None = None


def _limiter(settings: Settings) -> Any:
    """One limiter store per worker process: Redis when configured, so the sweep spends from the
    same buckets the API's requests do."""
    global _sweep_limiter
    if _sweep_limiter is None:
        from felix.security.rate_limit import build_rate_limiter_backend

        _sweep_limiter = build_rate_limiter_backend(settings)
    return _sweep_limiter


async def run_upstream_checks(
    settings: Settings,
    *,
    limiter: Any | None = None,
    deps: importer.ImportDeps | None = None,
    batch: int = SWEEP_BATCH,
) -> dict[str, int]:
    """Check up to ``batch`` imported skills whose last check is older than
    `FELIX_SKILL_IMPORT_CHECK_HOURS` (never-checked first), across tenants, recording each. Off
    when the setting is 0.

    Each check stamps a sighting, so a skill's cooldown runs whether or not anyone asks. Each is
    charged to its tenant's budget and the deployment's, at `SWEEP_SHARE` of each: a tenant past
    its share is skipped for the rest of the tick, and the deployment's share ends the tick. One
    sweep at a time across workers (`quality_store.sweep_lock`, its own lease row), renewed before
    every check. A skill that is no longer an import is forgotten."""
    from felix.skills.quality_store import sweep_lock

    counts = {"checked": 0, "updates": 0, "failed": 0, "forgotten": 0, "budget_stopped": 0, "skipped": 0}
    hours = settings.skill_import_check_hours
    if not hours:
        return counts
    base = deps or importer.ImportDeps()
    limiter = limiter or _limiter(settings)
    store = get_upstream_store(settings)
    async with sweep_lock(settings, name=SWEEP_LEASE, lease_ms=SWEEP_LEASE_MS) as lease:
        if lease is None:
            counts["skipped"] = 1
            return counts
        spent: set[str] = set()
        for row in await store.due(checked_by=base.clock() - hours * HOUR_MS, limit=batch):
            tenant_id, name = str(row["tenant_id"]), str(row["name"])
            if tenant_id in spent:
                continue
            if not await lease.renew():
                logger.warning("skill_upstream: the sweep lease was taken over; stopping after %s", counts)
                break
            try:
                _, head = await imported_head(settings, tenant_id, name)
            except library.SkillNotFound, SkillNotImported:
                await store.forget(tenant_id, name)
                counts["forgotten"] += 1
                continue
            charge = importer.github_call_budget(limiter, settings, tenant_id, share=SWEEP_SHARE)
            try:
                state = await _check_one(settings, tenant_id, head, replace(base, charge=charge), {})
            except ImportBudgetExhausted as exc:
                counts["budget_stopped"] += 1
                if exc.deployment:
                    break
                spent.add(tenant_id)
                continue
            except SkillImportError as exc:
                logger.info("skill_upstream: %s/%s refused: %s", tenant_id, name, exc.code)
                await _record_failure(settings, tenant_id, head, exc, base.clock())
                counts["failed"] += 1
                continue
            counts["checked"] += 1
            counts["updates"] += state["upstream_tree_hash"] != head.get("origin_tree_hash")
    return counts


__all__ = [
    "LISTING_SECONDS",
    "MAX_OUTDATED",
    "SWEEP_BATCH",
    "SWEEP_LEASE",
    "SWEEP_SHARE",
    "check_upstream",
    "describe",
    "diff_versions",
    "imported_head",
    "outdated",
    "run_upstream_checks",
    "update_skill",
]
