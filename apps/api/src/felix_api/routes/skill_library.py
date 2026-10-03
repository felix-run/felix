"""The tenant skill library, for an operator: browse, review, author, publish, roll back.

`/skills/{manifest}` answers "what can this manifest's model reach". This answers "what is in
the tenant's library, and what is waiting on a person" -- every skill an agent drafted
(`create_skill`, `update_skill`) or an operator saved, with each version's review record. A
separate prefix rather than `/skills/library`, which `/skills/{manifest}` would swallow.

Reads need `skills:read`; anything that changes the library needs `skills:write`, which
implies the read. The tenant is the authenticated principal's, never the request's, and the
principal is who every change is audited to. All state changes go through `skills/library.py`,
so a publish here passes the same gate an agent's does: the bytes are re-read against their
saved digests and re-scanned, and a failing security scan blocks whoever asks.

Every refusal is a `SkillLibraryErrorOut` with the library's stable `code` (`_STATUS` maps it).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from felix.auth.mgmt import (
    SCOPE_SKILLS_READ,
    SCOPE_SKILLS_WRITE,
    require_mgmt_scopes,
    subject_from_request,
    tenant_id_from_request,
)
from felix.skills import library
from felix.skills.format import bundle_path_issue, is_valid_skill_name
from felix.skills.library_store import MAX_VERSIONS_LISTED, DraftCursor, get_skill_library_store

from felix_api.routes._skill_library_models import (
    BundleIn,
    BundleIssueOut,
    CreateSkillIn,
    NewVersionIn,
    RejectIn,
    ReviewQueueOut,
    SkillArchivedOut,
    SkillDetailOut,
    SkillFileOut,
    SkillLibraryErrorOut,
    SkillListOut,
    SkillPolicyOut,
    SkillPreviewOut,
    SkillVersionDetailOut,
    SkillVersionOut,
    SkillWriteOut,
)

if TYPE_CHECKING:  # imports stay lazy at runtime; the annotations are the point
    from felix.config import Settings

router = APIRouter(tags=["Skill library"])

# Probes one listing runs at once (`shadows_operator_upload` is one object-store HEAD per key).
_PROBE_CONCURRENCY = 16


# -- refusals --------------------------------------------------------------------------------

# `SkillLibraryError.code` → status. 409 is "the library's state disagrees with the request";
# 422 is "this content can never be accepted as sent".
_STATUS: dict[str, int] = {
    "invalid_bundle": 422,
    "name_reserved": 422,
    # The name is the host's: no edit of the bundle makes it the library's.
    "name_shadows_host_skill": 409,
    "not_found": 404,
    "skill_exists": 409,
    "version_conflict": 409,
    "parent_changed": 409,
    # The queue drains as drafts are decided, so a retry later can succeed. Only agent drafts
    # are capped; an operator save through these routes never is.
    "pending_cap_reached": 429,
    # Versions are never deleted, so waiting does not help: the state is the obstacle.
    "version_cap_reached": 409,
    "publish_blocked": 422,
    # The object store no longer holds the bytes the row recorded. Nothing the caller sends
    # fixes that and a retry does not either: a server-side integrity failure, so a 5xx that
    # pages someone, under a code that says which.
    "version_corrupt": 500,
}

_ERRORS: dict[int | str, dict[str, Any]] = {
    status: {"model": SkillLibraryErrorOut} for status in sorted(set(_STATUS.values()))
}


def _error(status: int, code: str, message: str, **extra: Any) -> JSONResponse:
    body = SkillLibraryErrorOut(error=code, message=message, **extra)
    return JSONResponse(body.model_dump(exclude_none=True), status_code=status)


def _refusal(exc: library.SkillLibraryError) -> JSONResponse:
    extra: dict[str, Any] = {}
    if isinstance(exc, library.SkillBundleInvalid):
        extra["issues"] = [BundleIssueOut(path=i.path, message=i.message) for i in exc.issues[:50]]
    if isinstance(exc, library.SkillPublishBlocked):
        extra["reasons"] = list(exc.reasons)
    return _error(_STATUS.get(exc.code, 409), exc.code, str(exc), **extra)


def _not_found(what: str) -> JSONResponse:
    return _error(404, "not_found", f"{what} is not in the library")


def _addressable(name: str, version: str | None = None) -> bool:
    """A name and version that could be in the library at all. Anything else is a 404 before
    a store is asked -- and before a path segment reaches an object key."""
    if not is_valid_skill_name(name):
        return False
    return version is None or bool(library.VERSION_RE.match(version))


# -- shared reads ----------------------------------------------------------------------------


def _ctx(request: Request, scope: str) -> tuple[Settings, str]:
    require_mgmt_scopes(request, scope)
    return request.app.state.settings, tenant_id_from_request(request)


def _object_store(settings: Settings) -> Any:
    from felix.storage import get_object_store

    return get_object_store(settings)


async def _shadows(
    settings: Settings, tenant_id: str, name: str, versions: list[str | None], gate: asyncio.Semaphore
) -> bool:
    async with gate:
        return await library.shadows_operator_upload(
            settings, tenant_id, name, versions, object_store=_object_store(settings)
        )


async def _version_detail(settings: Settings, tenant_id: str, row: dict[str, Any]) -> dict[str, Any]:
    name, version = str(row["name"]), str(row["version"])
    files = await get_skill_library_store(settings).list_files(tenant_id, name, version)
    shadows = await library.shadows_operator_upload(
        settings, tenant_id, name, [version], object_store=_object_store(settings)
    )
    return {**row, "files": files, "shadows_operator_upload": shadows}


def _encode_cursor(row: dict[str, Any]) -> str:
    # Neither a skill name nor a version holds a colon, so the split back is unambiguous.
    return f"{row['created_at']}:{row['name']}:{row['version']}"


def _decode_cursor(cursor: str | None) -> DraftCursor | None:
    if not cursor:
        return None
    at, _, rest = cursor.partition(":")
    name, _, version = rest.partition(":")
    if not at.isdigit() or not name or not version:
        return None
    return int(at), name, version


# -- reads -----------------------------------------------------------------------------------


def _matches(
    skill: dict[str, Any], summary: dict[str, Any], *, status: str | None, source: str | None
) -> bool:
    """Whether a listed skill passes the listing's filters (`list_library`)."""
    latest = summary.get("latest") or {}
    if source is not None and latest.get("source") != source:
        return False
    pending = int(summary.get("pending") or 0)
    if status == "live":
        return skill["live_version"] is not None
    if status == "draft":
        return pending > 0
    if status == "archived":
        return skill["live_version"] is None and pending == 0
    return True


@router.get("", response_model=SkillListOut, responses=_ERRORS)
async def list_library(
    request: Request,
    status: Literal["live", "draft", "archived"] | None = Query(
        default=None,
        description="`live`: has a live version. `draft`: has drafts awaiting review. `archived`: neither.",
    ),
    source: Literal["agent", "operator"] | None = Query(
        default=None, description="Who wrote the newest version."
    ),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=64),
) -> Any:
    """The tenant's library skills by name, each with its live and newest version.

    Pages by name. The filters apply to a page after it is read, so a filtered page can be
    short; keep following `next_cursor` until it is null.
    """
    settings, tenant_id = _ctx(request, SCOPE_SKILLS_READ)
    lib = get_skill_library_store(settings)
    skills = await lib.list_skills(tenant_id, limit=limit, after=cursor or None)
    summaries = await lib.summarize(tenant_id, [s["name"] for s in skills])

    empty: dict[str, Any] = {"latest": None, "pending": 0}
    kept = [
        (s, summaries.get(s["name"], empty))
        for s in skills
        if _matches(s, summaries.get(s["name"], empty), status=status, source=source)
    ]
    gate = asyncio.Semaphore(_PROBE_CONCURRENCY)
    shadows = await asyncio.gather(
        *(
            _shadows(
                settings,
                tenant_id,
                s["name"],
                [s["live_version"], (summary.get("latest") or {}).get("version")],
                gate,
            )
            for s, summary in kept
        )
    )
    items = [
        {
            **s,
            "latest": summary.get("latest"),
            "pending_drafts": summary.get("pending", 0),
            "shadows_operator_upload": shadow,
        }
        for (s, summary), shadow in zip(kept, shadows, strict=True)
    ]
    next_cursor = skills[-1]["name"] if len(skills) == limit else None
    return {"items": items, "next_cursor": next_cursor}


@router.get("/review", response_model=ReviewQueueOut, responses=_ERRORS)
async def review_queue(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=128),
) -> Any:
    """Every draft in the tenant awaiting a decision, oldest first, across skills."""
    settings, tenant_id = _ctx(request, SCOPE_SKILLS_READ)
    lib = get_skill_library_store(settings)
    drafts = await lib.list_drafts(tenant_id, limit=limit, after=_decode_cursor(cursor))
    names = sorted({str(d["name"]) for d in drafts})
    skills = await asyncio.gather(*(lib.get_skill(tenant_id, n) for n in names))
    live = {n: (s or {}).get("live_version") for n, s in zip(names, skills, strict=True)}
    items = [{**d, "live_version": live.get(str(d["name"]))} for d in drafts]
    next_cursor = _encode_cursor(drafts[-1]) if len(drafts) == limit else None
    return {"items": items, "next_cursor": next_cursor}


@router.get("/policy", response_model=SkillPolicyOut, responses=_ERRORS)
async def publish_policy(request: Request) -> Any:
    """The gate every publish and rollback passes. From settings for now
    (`FELIX_SKILL_PUBLISH_MIN_QUALITY`, `FELIX_SKILL_PUBLISH_BLOCK_ON_ADVISORY`)."""
    settings, _ = _ctx(request, SCOPE_SKILLS_READ)
    return library.publish_policy(settings)


@router.get("/{name}", response_model=SkillDetailOut, responses=_ERRORS)
async def get_library_skill(name: str, request: Request) -> Any:
    """One skill and every version it holds (metadata only; files are read per version)."""
    settings, tenant_id = _ctx(request, SCOPE_SKILLS_READ)
    if not _addressable(name):
        return _not_found(name)
    lib = get_skill_library_store(settings)
    skill = await lib.get_skill(tenant_id, name)
    if skill is None:
        return _not_found(name)
    versions = await lib.list_versions(tenant_id, name, limit=MAX_VERSIONS_LISTED)
    newest = versions[0]["version"] if versions else None
    shadows = await library.shadows_operator_upload(
        settings, tenant_id, name, [skill["live_version"], newest], object_store=_object_store(settings)
    )
    return {**skill, "versions": versions, "shadows_operator_upload": shadows}


@router.get("/{name}/versions/{version}", response_model=SkillVersionDetailOut, responses=_ERRORS)
async def get_library_version(name: str, version: str, request: Request) -> Any:
    """One version: its review record, the checks and scan findings it was saved with, and
    its files' digests."""
    settings, tenant_id = _ctx(request, SCOPE_SKILLS_READ)
    if not _addressable(name, version):
        return _not_found(f"{name}@{version}")
    row = await get_skill_library_store(settings).get_version(tenant_id, name, version)
    if row is None:
        return _not_found(f"{name}@{version}")
    return await _version_detail(settings, tenant_id, row)


def _text_type(path: str) -> str:
    if path.endswith(".md"):
        return "text/markdown; charset=utf-8"
    if path.endswith(".json"):
        return "application/json"
    return "text/plain; charset=utf-8"


@router.get("/{name}/versions/{version}/files/{path:path}", response_model=SkillFileOut, responses=_ERRORS)
async def get_library_file(name: str, version: str, path: str, request: Request) -> Any:
    """One file of a version, checked against the digest recorded when it was saved.

    Text is secret-redacted, as `GET /skills/{manifest}/{skill}` redacts a body: this is the
    lower scope, and an embedded credential must not ride out on it. A binary asset comes back
    base64 and unredacted -- there is no text in it to redact.
    """
    from felix.secrets import collected_secret_values, redact_text
    from felix.skills.binary import binary_asset_mime_type, is_binary_asset_path

    settings, tenant_id = _ctx(request, SCOPE_SKILLS_READ)
    if not _addressable(name, version):
        return _not_found(f"{name}@{version}")
    if path != "SKILL.md" and bundle_path_issue(path) is not None:
        return _error(422, "invalid_path", "not a bundle file path")
    files = await get_skill_library_store(settings).list_files(tenant_id, name, version)
    meta = next((f for f in files if f["path"] == path), None)
    if meta is None:
        return _not_found(f"{name}@{version}/{path}")
    try:
        content = await library.read_version_file(
            settings, tenant_id, name, version, path, object_store=_object_store(settings)
        )
    except library.SkillLibraryError as exc:
        return _refusal(exc)
    if content is None:
        return _not_found(f"{name}@{version}/{path}")
    binary = is_binary_asset_path(path)
    return {
        "path": path,
        "encoding": "base64" if binary else "utf-8",
        "content_type": binary_asset_mime_type(path) if binary else _text_type(path),
        "content": content if binary else redact_text(content, collected_secret_values(settings)),
        "sha256": meta["sha256"],
        "size": meta["size"],
    }


@router.get("/{name}/versions/{version}/preview", response_model=SkillPreviewOut, responses=_ERRORS)
async def preview_library_version(name: str, version: str, request: Request) -> Any:
    """Re-run review and the security scan on the stored bytes and say whether the publish
    policy would pass them. Read-only: no state changes and nothing is audited."""
    settings, tenant_id = _ctx(request, SCOPE_SKILLS_READ)
    if not _addressable(name, version):
        return _not_found(f"{name}@{version}")
    row = await get_skill_library_store(settings).get_version(tenant_id, name, version)
    if row is None:
        return _not_found(f"{name}@{version}")
    verdict = await library.evaluate_version(
        settings, tenant_id, name, version, object_store=_object_store(settings)
    )
    assessment = verdict["assessment"] or {}
    return {
        "name": name,
        "version": version,
        "status": row["status"],
        "valid": verdict["valid"],
        "validation_issues": verdict["validation_issues"],
        "quality_score": assessment.get("quality_score"),
        "review_checks": assessment.get("review_checks", []),
        "security_status": assessment.get("security_status"),
        "security_issues": assessment.get("security_issues", []),
        "policy_passes": not verdict["reasons"],
        "reasons": verdict["reasons"],
    }


# -- writes ----------------------------------------------------------------------------------


async def _saved(
    request: Request,
    settings: Settings,
    tenant_id: str,
    body: BundleIn,
    **save: Any,
) -> JSONResponse | dict[str, Any]:
    """Save a draft as the requesting principal, and publish it if asked."""
    by = subject_from_request(request)
    store = _object_store(settings)
    try:
        row = await library.save_draft(
            settings,
            tenant_id,
            files=body.files,
            provenance=library.DraftProvenance(
                source="operator", author=by, reason=body.reason, principal=by
            ),
            object_store=store,
            **save,
        )
    except library.SkillLibraryError as exc:
        return _refusal(exc)
    lib = get_skill_library_store(settings)
    # Read back, so the response carries every column the store defaults, as a later GET would.
    stored = await lib.get_version(tenant_id, row["name"], row["version"])
    row = {**(stored or row), "shadows_operator_upload": row["shadows_operator_upload"]}
    published, blocked = False, None
    if body.publish:
        try:
            row = {
                **await library.publish(
                    settings, tenant_id, row["name"], row["version"], by=by, object_store=store
                ),
                "shadows_operator_upload": row["shadows_operator_upload"],
            }
            published = True
        except library.SkillPublishBlocked as exc:
            blocked = list(exc.reasons)
        except library.SkillLibraryError as exc:
            blocked = [str(exc)]
    files = await lib.list_files(tenant_id, row["name"], row["version"])
    return {**row, "files": files, "published": published, "publish_blocked": blocked}


@router.post("", status_code=201, response_model=SkillWriteOut, responses=_ERRORS)
async def create_library_skill(body: CreateSkillIn, request: Request) -> Any:
    """Save a new skill as an operator draft (version 0.1.0), and publish it if `publish`.

    The name is the SKILL.md's own. 409 `skill_exists` if the library already holds it --
    `PUT /{name}/versions` adds a version.
    """
    settings, tenant_id = _ctx(request, SCOPE_SKILLS_WRITE)
    result = await _saved(request, settings, tenant_id, body, expect_newest=None)
    return result


@router.put("/{name}/versions", status_code=201, response_model=SkillWriteOut, responses=_ERRORS)
async def save_library_version(name: str, body: NewVersionIn, request: Request) -> Any:
    """Save a new version of an existing skill, edited from `parent_version`.

    `parent_version` must be the skill's newest version (409 `parent_changed` otherwise). The
    new version is `version` if given -- it must be newer than every existing one -- or the
    newest bumped by `bump` (default patch).
    """
    settings, tenant_id = _ctx(request, SCOPE_SKILLS_WRITE)
    if not _addressable(name):
        return _not_found(name)
    if await get_skill_library_store(settings).get_skill(tenant_id, name) is None:
        return _not_found(name)
    return await _saved(
        request,
        settings,
        tenant_id,
        body,
        name=name,
        parent=body.parent_version,
        expect_newest=body.parent_version,
        version=body.version,
        bump=body.bump or "patch",
    )


async def _transition(request: Request, name: str, version: str, action: str, note: str = "") -> Any:
    settings, tenant_id = _ctx(request, SCOPE_SKILLS_WRITE)
    if not _addressable(name, version):
        return _not_found(f"{name}@{version}")
    by = subject_from_request(request)
    try:
        if action == "reject":
            return await library.reject(settings, tenant_id, name, version, by=by, note=note)
        move = library.publish if action == "publish" else library.rollback
        return await move(settings, tenant_id, name, version, by=by, object_store=_object_store(settings))
    except library.SkillLibraryError as exc:
        return _refusal(exc)


@router.post("/{name}/versions/{version}/publish", response_model=SkillVersionOut, responses=_ERRORS)
async def publish_library_version(name: str, version: str, request: Request) -> Any:
    """Publish a draft: it becomes the live version, and the version it replaces is archived.
    422 `publish_blocked` with the gate's reasons when the policy refuses it."""
    return await _transition(request, name, version, "publish")


@router.post("/{name}/versions/{version}/rollback", response_model=SkillVersionOut, responses=_ERRORS)
async def rollback_library_version(name: str, version: str, request: Request) -> Any:
    """Make a version that was once live, live again -- through the same gate as a publish."""
    return await _transition(request, name, version, "rollback")


@router.post("/{name}/versions/{version}/reject", response_model=SkillVersionOut, responses=_ERRORS)
async def reject_library_version(name: str, version: str, body: RejectIn, request: Request) -> Any:
    """Archive a draft without publishing it, recording the note."""
    return await _transition(request, name, version, "reject", note=body.note)


@router.delete("/{name}", response_model=SkillArchivedOut, responses=_ERRORS)
async def archive_library_skill(name: str, request: Request) -> Any:
    """Take a skill out of every catalog. Its versions and their bytes are kept, and a
    rollback brings one back."""
    settings, tenant_id = _ctx(request, SCOPE_SKILLS_WRITE)
    if not _addressable(name):
        return _not_found(name)
    try:
        return await library.archive_skill(settings, tenant_id, name, by=subject_from_request(request))
    except library.SkillLibraryError as exc:
        return _refusal(exc)
