"""The tenant skill library, for an operator: browse, review, author, publish, roll back.

`/skills/{manifest}` answers "what can this manifest's model reach". This answers "what is in
the tenant's library, and what is waiting on a person" -- every skill an agent drafted
(`create_skill`, `update_skill`) or an operator saved, with each version's review record. A
separate prefix rather than `/skills/library`, which `/skills/{manifest}` would swallow.

Collection-wide reads live under `/-/` (`/-/review`, `/-/policy`). `-` cannot be a skill name,
so every name stays addressable as `/{name}` and no name has to be reserved.

Reads need `skills:read`; anything that changes the library needs `skills:write`, which
implies the read. The tenant is the authenticated principal's, never the request's, and the
principal is who every change is audited to. All state changes go through `skills/library.py`,
so a publish here passes the same gate an agent's does: the bytes are re-read against their
saved digests and re-scanned, and a failing security scan blocks whoever asks.

Every text field that came from a saver or a scan -- reason, description, decision note,
review-check and security-issue messages -- is secret-redacted on the way out, as file bodies
are: this is the read scope, and an agent writes some of those fields.

Every refusal is a `SkillLibraryErrorOut` with the library's stable `code` (`_STATUS` maps it).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
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
from felix.skills.library_store import (
    MAX_VERSIONS_LISTED,
    DraftCursor,
    SkillLibraryStore,
    get_skill_library_store,
)
from felix.skills.publish_gate import publish_policy

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
    status: {"model": SkillLibraryErrorOut} for status in sorted({*_STATUS.values(), 422})
}


def _error_body(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return SkillLibraryErrorOut(error=code, message=message, **extra).model_dump(exclude_none=True)


def _error(status: int, code: str, message: str, **extra: Any) -> JSONResponse:
    return JSONResponse(_error_body(code, message, **extra), status_code=status)


def _refusal_body(exc: library.SkillLibraryError) -> dict[str, Any]:
    extra: dict[str, Any] = {}
    if isinstance(exc, library.SkillBundleInvalid):
        extra["issues"] = [BundleIssueOut(path=i.path, message=i.message) for i in exc.issues[:50]]
    if isinstance(exc, library.SkillPublishBlocked):
        extra["reasons"] = list(exc.reasons)
    return _error_body(exc.code, str(exc), **extra)


def _refusal(exc: library.SkillLibraryError) -> JSONResponse:
    # An unmapped code is a library refusal this module was never taught: a server bug, so 500
    # rather than a guess that reads to a client as "your request conflicted".
    return JSONResponse(_refusal_body(exc), status_code=_STATUS.get(exc.code, 500))


def _not_found(what: str) -> JSONResponse:
    return _error(404, "not_found", f"{what} is not in the library")


def _addressable(name: str, version: str | None = None) -> bool:
    """A name and version that could be in the library at all. Anything else is a 404 before
    a store is asked -- and before a path segment reaches an object key."""
    if not is_valid_skill_name(name):
        return False
    return version is None or bool(library.VERSION_RE.match(version))


# -- one request's view of the library -------------------------------------------------------


@dataclass(slots=True)
class _Lib:
    """What every handler needs, resolved once per request: settings, the caller's tenant, the
    stores, and the secret values responses are redacted against."""

    settings: Settings
    tenant_id: str
    lib: SkillLibraryStore
    store: Any
    secrets: list[str]

    def redact(self, row: dict[str, Any]) -> dict[str, Any]:
        """A version or skill row with every saver- or scan-written text field redacted."""
        from felix.secrets import redact_json, redact_text

        out = dict(row)
        for key in ("reason", "description", "decision_note"):
            if isinstance(out.get(key), str):
                out[key] = redact_text(out[key], self.secrets)
        for key in ("review_checks", "security_issues", "validation_issues"):
            if isinstance(out.get(key), list):
                out[key] = redact_json(out[key], self.secrets)
        return out

    async def shadows(self, name: str, versions: list[str | None]) -> bool:
        return await library.shadows_operator_upload(
            self.settings, self.tenant_id, name, versions, object_store=self.store
        )


def _ctx(request: Request, scope: str) -> _Lib:
    from felix.secrets import collected_secret_values
    from felix.storage import get_object_store

    require_mgmt_scopes(request, scope)
    settings = request.app.state.settings
    return _Lib(
        settings=settings,
        tenant_id=tenant_id_from_request(request),
        lib=get_skill_library_store(settings),
        store=get_object_store(settings),
        secrets=collected_secret_values(settings),
    )


async def _version_detail(ctx: _Lib, row: dict[str, Any]) -> dict[str, Any]:
    name, version = str(row["name"]), str(row["version"])
    files = await ctx.lib.list_files(ctx.tenant_id, name, version)
    shadows = await ctx.shadows(name, [version])
    return {**ctx.redact(row), "files": files, "shadows_operator_upload": shadows}


def _encode_cursor(row: dict[str, Any]) -> str:
    # Neither a skill name nor a version holds a colon, so the split back is unambiguous.
    return f"{row['created_at']}:{row['name']}:{row['version']}"


def _decode_cursor(cursor: str) -> DraftCursor | None:
    """The `(created_at, name, version)` a review-queue cursor names, or None when it is not
    one this route issued. ASCII digits only: `str.isdigit` accepts `²`, which `int` refuses."""
    at, _, rest = cursor.partition(":")
    name, _, version = rest.partition(":")
    if not at or not at.isascii() or not at.isdigit() or not name or not version:
        return None
    return int(at), name, version


def _bad_cursor() -> JSONResponse:
    return _error(422, "invalid_cursor", "cursor is not one this route issued; start from the first page")


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
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=64),
) -> Any:
    """The tenant's library skills by name, each with its live and newest version.

    Pages by name. The filters apply to a page after it is read, so a filtered page can be
    short -- even empty -- and still have a next one; keep following `next_cursor` until null.
    """
    ctx = _ctx(request, SCOPE_SKILLS_READ)
    skills = await ctx.lib.list_skills(ctx.tenant_id, limit=limit, after=cursor or None)
    summaries = await ctx.lib.summarize(ctx.tenant_id, [s["name"] for s in skills])
    empty: dict[str, Any] = {"latest": None, "pending": 0}
    kept = [
        (s, summaries.get(s["name"], empty))
        for s in skills
        if _matches(s, summaries.get(s["name"], empty), status=status, source=source)
    ]
    gate = asyncio.Semaphore(_PROBE_CONCURRENCY)

    async def probe(skill: dict[str, Any], summary: dict[str, Any]) -> bool:
        async with gate:
            return await ctx.shadows(
                skill["name"], [skill["live_version"], (summary["latest"] or {}).get("version")]
            )

    shadows = await asyncio.gather(*(probe(s, summary) for s, summary in kept))
    items = [
        {
            **s,
            "latest": summary["latest"],
            "pending_drafts": summary["pending"],
            "shadows_operator_upload": shadow,
        }
        for (s, summary), shadow in zip(kept, shadows, strict=True)
    ]
    next_cursor = skills[-1]["name"] if len(skills) == limit else None
    return {"items": items, "next_cursor": next_cursor}


@router.get("/-/review", response_model=ReviewQueueOut, responses=_ERRORS)
async def review_queue(
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=128),
) -> Any:
    """Every draft in the tenant awaiting a decision, oldest first, across skills."""
    ctx = _ctx(request, SCOPE_SKILLS_READ)
    after = _decode_cursor(cursor) if cursor else None
    if cursor and after is None:
        return _bad_cursor()
    drafts = await ctx.lib.list_drafts(ctx.tenant_id, limit=limit, after=after)
    skills = await ctx.lib.get_skills(ctx.tenant_id, {str(d["name"]) for d in drafts})
    items = [
        {**ctx.redact(d), "live_version": (skills.get(str(d["name"])) or {}).get("live_version")}
        for d in drafts
    ]
    next_cursor = _encode_cursor(drafts[-1]) if len(drafts) == limit else None
    return {"items": items, "next_cursor": next_cursor}


@router.get("/-/policy", response_model=SkillPolicyOut, responses=_ERRORS)
async def get_publish_policy(request: Request) -> Any:
    """The gate every publish and rollback passes. From settings for now
    (`FELIX_SKILL_PUBLISH_MIN_QUALITY`, `FELIX_SKILL_PUBLISH_BLOCK_ON_ADVISORY`)."""
    ctx = _ctx(request, SCOPE_SKILLS_READ)
    return asdict(publish_policy(ctx.settings, ctx.tenant_id))


@router.get("/{name}", response_model=SkillDetailOut, responses=_ERRORS)
async def get_library_skill(name: str, request: Request) -> Any:
    """One skill and every version it holds (metadata only; files are read per version)."""
    ctx = _ctx(request, SCOPE_SKILLS_READ)
    if not _addressable(name):
        return _not_found(name)
    skill = await ctx.lib.get_skill(ctx.tenant_id, name)
    if skill is None:
        return _not_found(name)
    versions = await ctx.lib.list_versions(ctx.tenant_id, name, limit=MAX_VERSIONS_LISTED)
    newest = versions[0]["version"] if versions else None
    shadows = await ctx.shadows(name, [skill["live_version"], newest])
    return {**skill, "versions": [ctx.redact(v) for v in versions], "shadows_operator_upload": shadows}


@router.get("/{name}/versions/{version}", response_model=SkillVersionDetailOut, responses=_ERRORS)
async def get_library_version(name: str, version: str, request: Request) -> Any:
    """One version: its review record, the checks and scan findings it was saved with, and
    its files' digests."""
    ctx = _ctx(request, SCOPE_SKILLS_READ)
    if not _addressable(name, version):
        return _not_found(f"{name}@{version}")
    row = await ctx.lib.get_version(ctx.tenant_id, name, version)
    if row is None:
        return _not_found(f"{name}@{version}")
    return await _version_detail(ctx, row)


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
    from felix.secrets import redact_text
    from felix.skills.binary import binary_asset_mime_type, is_binary_asset_path

    ctx = _ctx(request, SCOPE_SKILLS_READ)
    if not _addressable(name, version):
        return _not_found(f"{name}@{version}")
    if path != "SKILL.md" and bundle_path_issue(path) is not None:
        return _error(422, "invalid_path", "not a bundle file path")
    files = await ctx.lib.list_files(ctx.tenant_id, name, version)
    meta = next((f for f in files if f["path"] == path), None)
    if meta is None:
        return _not_found(f"{name}@{version}/{path}")
    try:
        content = await library.read_version_file(
            ctx.settings, ctx.tenant_id, name, version, path, object_store=ctx.store
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
        "content": content if binary else redact_text(content, ctx.secrets),
        "sha256": meta["sha256"],
        "size": meta["size"],
    }


@router.get("/{name}/versions/{version}/preview", response_model=SkillPreviewOut, responses=_ERRORS)
async def preview_library_version(name: str, version: str, request: Request) -> Any:
    """Re-run review and the security scan on the stored bytes and say whether the publish
    policy would pass them. Read-only: no state changes and nothing is audited."""
    ctx = _ctx(request, SCOPE_SKILLS_READ)
    if not _addressable(name, version):
        return _not_found(f"{name}@{version}")
    row = await ctx.lib.get_version(ctx.tenant_id, name, version)
    if row is None:
        return _not_found(f"{name}@{version}")
    verdict = await library.evaluate_version(
        ctx.settings,
        ctx.tenant_id,
        name,
        version,
        policy=publish_policy(ctx.settings, ctx.tenant_id),
        object_store=ctx.store,
    )
    assessment = verdict.assessment
    return ctx.redact(
        {
            "name": name,
            "version": version,
            "status": row["status"],
            "valid": verdict.valid,
            "validation_issues": verdict.validation_issues,
            "quality_score": assessment.quality_score if assessment else None,
            "review_checks": assessment.review_checks if assessment else [],
            "security_status": assessment.security_status if assessment else None,
            "security_issues": assessment.security_issues if assessment else [],
            "policy_passes": verdict.passes,
            "reasons": verdict.reasons,
        }
    )


# -- writes ----------------------------------------------------------------------------------


async def _saved(request: Request, ctx: _Lib, body: BundleIn, **save: Any) -> JSONResponse | dict[str, Any]:
    """Save a draft as the requesting principal, and publish it if asked."""
    by = subject_from_request(request)
    try:
        saved = await library.save_draft(
            ctx.settings,
            ctx.tenant_id,
            files=body.files,
            provenance=library.DraftProvenance(
                source="operator", author=by, reason=body.reason, principal=by
            ),
            object_store=ctx.store,
            **save,
        )
    except library.SkillLibraryError as exc:
        return _refusal(exc)
    name, version = str(saved["name"]), str(saved["version"])
    published, blocked = False, None
    if body.publish:
        try:
            await library.publish(ctx.settings, ctx.tenant_id, name, version, by=by, object_store=ctx.store)
            published = True
        except library.SkillLibraryError as exc:
            # The draft is saved either way; the refusal says why it did not also go live.
            blocked = _refusal_body(exc)
    # Read back, so the response carries every column the store defaults, as a later GET would.
    row = await ctx.lib.get_version(ctx.tenant_id, name, version) or saved
    files = await ctx.lib.list_files(ctx.tenant_id, name, version)
    return {
        **ctx.redact(row),
        "files": files,
        "shadows_operator_upload": saved["shadows_operator_upload"],
        "published": published,
        "publish_blocked": blocked,
    }


@router.post("", status_code=201, response_model=SkillWriteOut, responses=_ERRORS)
async def create_library_skill(body: CreateSkillIn, request: Request) -> Any:
    """Save a new skill as an operator draft (version 0.1.0), and publish it if `publish`.

    The name is the SKILL.md's own. 409 `skill_exists` if the library already holds it --
    `PUT /{name}/versions` adds a version.
    """
    ctx = _ctx(request, SCOPE_SKILLS_WRITE)
    return await _saved(request, ctx, body, expect_newest=library.MUST_NOT_EXIST)


@router.put("/{name}/versions", status_code=201, response_model=SkillWriteOut, responses=_ERRORS)
async def save_library_version(name: str, body: NewVersionIn, request: Request) -> Any:
    """Save a new version of an existing skill, edited from `parent_version`.

    `parent_version` must be the skill's newest version (409 `parent_changed` otherwise). The
    new version is `version` if given -- it must be newer than every existing one -- or the
    newest bumped by `bump` (default patch).
    """
    ctx = _ctx(request, SCOPE_SKILLS_WRITE)
    if not _addressable(name) or await ctx.lib.get_skill(ctx.tenant_id, name) is None:
        return _not_found(name)
    return await _saved(
        request,
        ctx,
        body,
        name=name,
        parent=body.parent_version,
        expect_newest=body.parent_version,
        version=body.version,
        bump=body.bump or "patch",
    )


async def _transition(
    request: Request,
    name: str,
    version: str,
    move: Callable[[_Lib, str], Awaitable[dict[str, Any]]],
) -> Any:
    """Run one state change on ``name@version`` as the caller, mapping a refusal."""
    ctx = _ctx(request, SCOPE_SKILLS_WRITE)
    if not _addressable(name, version):
        return _not_found(f"{name}@{version}")
    try:
        return ctx.redact(await move(ctx, subject_from_request(request)))
    except library.SkillLibraryError as exc:
        return _refusal(exc)


@router.post("/{name}/versions/{version}/publish", response_model=SkillVersionOut, responses=_ERRORS)
async def publish_library_version(name: str, version: str, request: Request) -> Any:
    """Publish a draft: it becomes the live version, and the version it replaces is archived.
    422 `publish_blocked` with the gate's reasons when the policy refuses it."""

    async def move(ctx: _Lib, by: str) -> dict[str, Any]:
        return await library.publish(
            ctx.settings, ctx.tenant_id, name, version, by=by, object_store=ctx.store
        )

    return await _transition(request, name, version, move)


@router.post("/{name}/versions/{version}/rollback", response_model=SkillVersionOut, responses=_ERRORS)
async def rollback_library_version(name: str, version: str, request: Request) -> Any:
    """Make a version that was once live, live again -- through the same gate as a publish."""

    async def move(ctx: _Lib, by: str) -> dict[str, Any]:
        return await library.rollback(
            ctx.settings, ctx.tenant_id, name, version, by=by, object_store=ctx.store
        )

    return await _transition(request, name, version, move)


@router.post("/{name}/versions/{version}/reject", response_model=SkillVersionOut, responses=_ERRORS)
async def reject_library_version(name: str, version: str, body: RejectIn, request: Request) -> Any:
    """Archive a draft without publishing it, recording the note."""

    async def move(ctx: _Lib, by: str) -> dict[str, Any]:
        return await library.reject(ctx.settings, ctx.tenant_id, name, version, by=by, note=body.note)

    return await _transition(request, name, version, move)


@router.delete("/{name}", response_model=SkillArchivedOut, responses=_ERRORS)
async def archive_library_skill(name: str, request: Request) -> Any:
    """Take a skill out of every catalog. Its versions and their bytes are kept, and a
    rollback brings one back."""
    ctx = _ctx(request, SCOPE_SKILLS_WRITE)
    if not _addressable(name):
        return _not_found(name)
    try:
        return await library.archive_skill(
            ctx.settings, ctx.tenant_id, name, by=subject_from_request(request)
        )
    except library.SkillLibraryError as exc:
        return _refusal(exc)
