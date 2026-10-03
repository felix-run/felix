"""The tenant skill library: save a draft, publish it through a gate, roll back, reject, archive.

A skill body is returned by `activate_skill` as *instructions* -- a higher-trust surface than
recalled memory, which is fenced as reference. So an agent's skill is a draft until something
publishes it, and publishing runs a gate no setting can open past: a failing security scan
blocks, always. `FELIX_SKILL_PUBLISH_MIN_QUALITY` and `FELIX_SKILL_PUBLISH_BLOCK_ON_ADVISORY`
raise the bar from there. Every state change is an audit event.

Versions are immutable semver. A save reserves its version row first and writes the bytes
after, so the primary key is the lock: two saves racing to `0.1.1` collide on the row rather
than both writing the same object keys, where the loser's bytes would have replaced the
winner's under a row that described the winner. Rollback is a pointer move to a version that
was once published.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from collections.abc import Mapping
from dataclasses import asdict
from functools import cmp_to_key
from typing import Any, Literal

from felix.config import Settings
from felix.skills.binary import decode_base64, encode_base64, is_binary_asset_path
from felix.skills.format import ValidationIssue, validate_skill_bundle
from felix.skills.library_store import (
    SkillLibraryStore,
    SkillStateConflict,
    SkillVersionExists,
    get_skill_library_store,
)
from felix.skills.review import review_skill_bundle
from felix.skills.security import scan_skill_security
from felix.skills.semver import SemverBump, compare_semver, resolve_next_semver

logger = logging.getLogger("felix.skills.library")

now_ms = lambda: int(time.time() * 1000)

SkillSourceKind = Literal["agent", "operator"]

# Strict `major.minor.patch`: a version is interpolated into an object key, and the loader's
# own key-segment rule (`loader._VERSION_RE`) is looser than this.
VERSION_RE = re.compile(r"^\d{1,6}\.\d{1,6}\.\d{1,6}\Z")
# Saves that lose a race to the same next version are retried this many times before the
# caller is told; each retry re-reads the versions and bumps past the winner.
_SAVE_ATTEMPTS = 3
_REASON_LIMIT = 2000


class SkillLibraryError(Exception):
    """Base for every refusal the library makes. `code` is stable; routes map it to a status."""

    code = "skill_library_error"


class SkillBundleInvalid(SkillLibraryError):
    code = "invalid_bundle"

    def __init__(self, issues: list[ValidationIssue]) -> None:
        self.issues = issues
        super().__init__("; ".join(f"{i.path}: {i.message}" for i in issues) or "invalid bundle")


class SkillNameShadowed(SkillLibraryError):
    """The name belongs to the host catalog (or an operator-uploaded object-store skill)."""

    code = "name_shadows_host_skill"


class SkillNotFound(SkillLibraryError):
    code = "not_found"


class SkillVersionConflict(SkillLibraryError):
    """The version exists, is not newer than every existing one, or changed state underneath."""

    code = "version_conflict"


class SkillPendingCapReached(SkillLibraryError):
    code = "pending_cap_reached"


class SkillPublishBlocked(SkillLibraryError):
    code = "publish_blocked"

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = reasons
        super().__init__("; ".join(reasons))


def _object_key(tenant_id: str, name: str, version: str, path: str) -> str:
    # The layout `skills/loader.py:load_skill_from_store` and `_library_skill` read.
    return f"skills/{tenant_id}/{name}/{version}/{path}"


def _object_store(settings: Settings, object_store: Any | None) -> Any:
    if object_store is not None:
        return object_store
    from felix.storage import get_object_store

    return get_object_store(settings)


def _audit(
    settings: Settings, tenant_id: str, event_type: str, row: Mapping[str, Any], *, by: str, **extra: Any
) -> None:
    """One event per state change, written straight to the audit store.

    Not `emit_agent_audit`: that needs a request context, and an operator publishing from a
    management route or a worker task has none. The payload names the version and how it
    scored, so the trail answers "what went live, who let it, and what did the scan say"
    without the version row, which a later rollback rewrites.
    """
    from felix.audit import store as audit_store

    status = str(extra.pop("status", "ok"))
    payload = {
        "skill": row.get("name"),
        "version": row.get("version"),
        "source": row.get("source"),
        "author": row.get("author"),
        "quality_score": row.get("quality_score"),
        "security_status": row.get("security_status"),
        **extra,
    }
    try:
        audit_store.record_event(
            settings,
            tenant_id,
            event_type,
            manifest_id=row.get("origin_manifest_id") or "",
            principal_subj=by,
            status=status,
            payload=payload,
        )
    except Exception:
        logger.warning("audit write failed for %s", event_type, exc_info=True)


async def host_owns(settings: Settings, tenant_id: str, name: str, object_store: Any | None = None) -> bool:
    """True when ``name`` is a host skill or an operator-uploaded object-store skill.

    The library may not save one. The catalog would keep serving the host's copy (host wins),
    so a shadowing save would be inert at best -- and at worst, under `skills_declared_only`
    or after the host drops the skill, a tenant's text would start answering to a name an
    operator chose and reviewed.
    """
    from felix.skills.loader import host_catalog

    if (await host_catalog()).get(name) is not None:
        return True
    store = _object_store(settings, object_store)
    for key in (f"skills/{tenant_id}/{name}/SKILL.md", f"skills/{name}/SKILL.md"):
        try:
            if await store.exists(key):
                return True
        except Exception:
            logger.warning("object store probe failed for %s", key, exc_info=True)
    return False


def _next_version(existing: list[str], *, explicit: str | None, bump: SemverBump) -> str:
    # By semver rather than by string, where `0.10.0` sorts before `0.9.0`.
    newest = max(existing, key=cmp_to_key(compare_semver), default=None)
    if explicit is not None:
        if not VERSION_RE.match(explicit):
            raise SkillVersionConflict(f"version {explicit!r} is not major.minor.patch")
        if newest is not None and compare_semver(explicit, newest) <= 0:
            raise SkillVersionConflict(f"version {explicit} must be newer than {newest}")
        return explicit
    return resolve_next_semver(newest, bump=bump)


def _assess(files: Mapping[str, str], name: str) -> dict[str, Any]:
    """Review and scan in one call, so one `to_thread` hop covers both."""
    review = review_skill_bundle(files, name)
    scan = scan_skill_security(files)
    return {
        "quality_score": review.score,
        "review_checks": [asdict(c) for c in review.checks],
        "security_status": scan.status,
        "security_issues": [asdict(i) for i in scan.issues],
    }


def _stored_bytes(path: str, content: str) -> bytes:
    # A binary asset arrives base64 (the bundle is text); the object store holds the bytes.
    return decode_base64(content) if is_binary_asset_path(path) else content.encode("utf-8")


async def _write_files(store: Any, tenant_id: str, name: str, version: str, files: Mapping[str, str]) -> None:
    for path, content in files.items():
        await store.put(_object_key(tenant_id, name, version, path), _stored_bytes(path, content))


def _file_rows(files: Mapping[str, str]) -> list[dict[str, Any]]:
    rows = []
    for path, content in sorted(files.items()):
        data = _stored_bytes(path, content)
        rows.append({"path": path, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)})
    return rows


async def _reserve(
    lib: SkillLibraryStore,
    tenant_id: str,
    row: dict[str, Any],
    files: list[dict[str, Any]],
    *,
    explicit: str | None,
    bump: SemverBump,
) -> dict[str, Any]:
    """Insert the version row under the next free version; the primary key settles a race."""
    for _ in range(_SAVE_ATTEMPTS):
        existing = await lib.version_ids(tenant_id, row["name"])
        row = {**row, "version": _next_version(existing, explicit=explicit, bump=bump)}
        try:
            await lib.insert_version(tenant_id, row, files, created_by=row["author"], at=row["created_at"])
            return row
        except SkillVersionExists as exc:
            if explicit is not None:
                raise SkillVersionConflict(f"version {explicit} already exists") from exc
    raise SkillVersionConflict("concurrent saves kept taking the next version; try again")


async def save_draft(
    settings: Settings,
    tenant_id: str,
    *,
    files: Mapping[str, str],
    source: SkillSourceKind,
    author: str,
    reason: str,
    name: str | None = None,
    origin_manifest_id: str | None = None,
    session_id: str | None = None,
    parent: str | None = None,
    version: str | None = None,
    bump: SemverBump = "patch",
    max_pending: int | None = None,
    object_store: Any | None = None,
) -> dict[str, Any]:
    """Validate, review and scan a bundle, then save it as a new immutable draft.

    ``name``, when given, must be the SKILL.md's own name. ``parent`` is the version this one
    was edited from (lineage, not the bump base: the bump is from the newest version, so a
    save is always newer than everything saved before it). ``max_pending`` caps how many
    undecided agent drafts one origin manifest may hold, so an agent in a loop fills a review
    queue only so far.
    """
    validation = await asyncio.to_thread(validate_skill_bundle, files, name)
    if not validation.valid or validation.frontmatter is None:
        raise SkillBundleInvalid(validation.errors)
    skill_name = validation.frontmatter.name
    store = _object_store(settings, object_store)
    if await host_owns(settings, tenant_id, skill_name, store):
        raise SkillNameShadowed(f"{skill_name!r} is a host skill; the library cannot replace it")

    lib = get_skill_library_store(settings)
    if source == "agent" and max_pending is not None and origin_manifest_id:
        pending = await lib.count_pending(tenant_id, origin_manifest_id)
        if pending >= max_pending:
            raise SkillPendingCapReached(
                f"{origin_manifest_id} already has {pending} drafts awaiting review (limit {max_pending})"
            )
    if parent is not None and await lib.get_version(tenant_id, skill_name, parent) is None:
        raise SkillNotFound(f"parent version {skill_name}@{parent} does not exist")

    row = {
        "name": skill_name,
        "parent_version": parent,
        "status": "draft",
        "source": source,
        "author": author,
        "origin_manifest_id": origin_manifest_id,
        "session_id": session_id,
        "reason": (reason or "")[:_REASON_LIMIT],
        "description": validation.frontmatter.description,
        **await asyncio.to_thread(_assess, files, skill_name),
        "created_at": now_ms(),
    }
    row = await _reserve(lib, tenant_id, row, _file_rows(files), explicit=version, bump=bump)
    try:
        await _write_files(store, tenant_id, skill_name, row["version"], files)
    except Exception:
        # The row is a draft, so nothing loaded it in the meantime; take it back out.
        await lib.delete_draft(tenant_id, skill_name, row["version"])
        raise
    _audit(
        settings, tenant_id, "skill_draft_saved", row, by=author, parent=parent, reason=row["reason"][:200]
    )
    return {**row, "tenant_id": tenant_id}


async def read_version_files(
    settings: Settings, tenant_id: str, name: str, version: str, *, object_store: Any | None = None
) -> dict[str, str]:
    """Every file of a saved version as bundle text (binary assets base64), checked against
    the digests recorded when it was saved. A missing or altered file raises."""
    lib = get_skill_library_store(settings)
    store = _object_store(settings, object_store)
    files: dict[str, str] = {}
    for meta in await lib.list_files(tenant_id, name, version):
        path = str(meta["path"])
        data = await store.get(_object_key(tenant_id, name, version, path))
        if data is None:
            raise SkillPublishBlocked([f"{path} is missing from the object store"])
        if hashlib.sha256(data).hexdigest() != meta["sha256"]:
            raise SkillPublishBlocked([f"{path} changed in the object store since it was saved"])
        files[path] = encode_base64(data) if is_binary_asset_path(path) else data.decode("utf-8")
    return files


def _policy_reasons(settings: Settings, assessment: Mapping[str, Any]) -> list[str]:
    reasons: list[str] = []
    status = assessment["security_status"]
    if status == "fail":
        found = [i["message"] for i in assessment["security_issues"] if i["severity"] in {"critical", "high"}]
        reasons.append("security scan failed: " + "; ".join(found[:5]))
    elif status == "advisory" and settings.skill_publish_block_on_advisory:
        reasons.append("security scan is advisory and FELIX_SKILL_PUBLISH_BLOCK_ON_ADVISORY is set")
    floor = int(settings.skill_publish_min_quality or 0)
    if assessment["quality_score"] < floor:
        reasons.append(f"quality score {assessment['quality_score']} is below the minimum {floor}")
    return reasons


async def _gate(settings: Settings, tenant_id: str, row: Mapping[str, Any], object_store: Any) -> None:
    """Re-read, re-validate and re-scan what is about to go live, then apply the policy.

    Re-run rather than trusting the row: the bytes are in a store an operator can write
    directly, and the scanner's rules may have grown since the draft was saved.
    """
    name, version = str(row["name"]), str(row["version"])
    files = await read_version_files(settings, tenant_id, name, version, object_store=object_store)
    validation = await asyncio.to_thread(validate_skill_bundle, files, name)
    if not validation.valid:
        raise SkillPublishBlocked(
            [f"bundle no longer validates: {i.path}: {i.message}" for i in validation.errors]
        )
    reasons = _policy_reasons(settings, await asyncio.to_thread(_assess, files, name))
    if reasons:
        raise SkillPublishBlocked(reasons)


# What each way of going live may start from. A publish takes a draft; a rollback takes a
# version that is, or was, live -- and `_make_live` also requires that it once went live.
_LIVE_FROM = {
    "skill_published": frozenset({"draft"}),
    "skill_rolled_back": frozenset({"archived", "published"}),
}


async def _make_live(
    settings: Settings,
    tenant_id: str,
    name: str,
    version: str,
    *,
    by: str,
    event: str,
    object_store: Any | None,
) -> dict[str, Any]:
    from_statuses = _LIVE_FROM[event]
    lib = get_skill_library_store(settings)
    row = await lib.get_version(tenant_id, name, version)
    if row is None:
        raise SkillNotFound(f"{name}@{version} does not exist")
    if row["status"] not in from_statuses:
        raise SkillVersionConflict(f"{name}@{version} is {row['status']}")
    if event == "skill_rolled_back" and row.get("published_at") is None:
        # A rejected draft is `archived` too; only a version that once went live may return.
        raise SkillVersionConflict(f"{name}@{version} was never published; publish it instead")
    try:
        await _gate(settings, tenant_id, row, _object_store(settings, object_store))
    except SkillPublishBlocked as exc:
        _audit(settings, tenant_id, event, row, by=by, status="blocked", reasons=exc.reasons)
        raise
    try:
        previous = await lib.publish(
            tenant_id, name, version, from_statuses=from_statuses, by=by, at=now_ms()
        )
    except SkillStateConflict as exc:
        raise SkillVersionConflict(f"{name}@{version} changed state while it was being published") from exc
    _audit(settings, tenant_id, event, row, by=by, previous=previous)
    return await lib.get_version(tenant_id, name, version) or row


async def publish(
    settings: Settings, tenant_id: str, name: str, version: str, *, by: str, object_store: Any | None = None
) -> dict[str, Any]:
    """Publish a draft: it becomes `live_version`, and the version it replaces is archived."""
    return await _make_live(
        settings,
        tenant_id,
        name,
        version,
        by=by,
        event="skill_published",
        object_store=object_store,
    )


async def rollback(
    settings: Settings, tenant_id: str, name: str, version: str, *, by: str, object_store: Any | None = None
) -> dict[str, Any]:
    """Make a once-published version live again, through the same gate as a publish."""
    return await _make_live(
        settings,
        tenant_id,
        name,
        version,
        by=by,
        event="skill_rolled_back",
        object_store=object_store,
    )


async def reject(
    settings: Settings, tenant_id: str, name: str, version: str, *, by: str, note: str
) -> dict[str, Any]:
    """Archive a draft without publishing it, recording why."""
    lib = get_skill_library_store(settings)
    row = await lib.get_version(tenant_id, name, version)
    if row is None:
        raise SkillNotFound(f"{name}@{version} does not exist")
    try:
        await lib.reject(tenant_id, name, version, by=by, note=(note or "")[:_REASON_LIMIT], at=now_ms())
    except SkillStateConflict as exc:
        raise SkillVersionConflict(f"{name}@{version} is not a draft") from exc
    _audit(settings, tenant_id, "skill_rejected", row, by=by, note=(note or "")[:200])
    return await lib.get_version(tenant_id, name, version) or row


async def archive_skill(settings: Settings, tenant_id: str, name: str, *, by: str) -> dict[str, Any]:
    """Take a skill out of every catalog: `live_version` is cleared, its history kept."""
    lib = get_skill_library_store(settings)
    try:
        previous = await lib.archive_skill(tenant_id, name, by=by, at=now_ms())
    except SkillStateConflict as exc:
        raise SkillNotFound(f"{name} is not in the library") from exc
    row = (await lib.get_version(tenant_id, name, previous) if previous else None) or {"name": name}
    _audit(settings, tenant_id, "skill_archived", row, by=by, previous=previous)
    return await lib.get_skill(tenant_id, name) or {"name": name, "live_version": None}


__all__ = [
    "VERSION_RE",
    "SkillBundleInvalid",
    "SkillLibraryError",
    "SkillNameShadowed",
    "SkillNotFound",
    "SkillPendingCapReached",
    "SkillPublishBlocked",
    "SkillVersionConflict",
    "archive_skill",
    "host_owns",
    "publish",
    "read_version_files",
    "reject",
    "rollback",
    "save_draft",
]
