"""The tenant skill library, for an operator: browse, review, author, publish, roll back.

`/skills/{manifest}` answers "what can this manifest's model reach". This answers "what is in
the tenant's library, and what is waiting on a person" -- every skill an agent drafted
(`create_skill`, `update_skill`) or an operator saved, with each version's review record. A
separate prefix rather than `/skills/library`, which `/skills/{manifest}` would swallow.

Collection-wide routes live under `/-/` (`/-/review`, `/-/policy`; `skill_quality.py` adds
`/-/feedback`). `-` cannot be a skill name, so every name stays addressable as `/{name}` and no name
has to be reserved. The tenant's publish policy is here: `PATCH /-/policy` tightens the deployment's
`FELIX_SKILL_PUBLISH_*` settings for the tenant (it can never loosen them), `DELETE` drops it.
Feedback and evaluations are `skill_quality.py`, under the same prefix.

Reads need `skills:read`; anything that changes the library needs `skills:write`, which
implies the read. The tenant is the authenticated principal's, never the request's, and the
principal is who every change is audited to. All state changes go through `skills/library.py`,
so a publish here passes the same gate an agent's does: the bytes are re-read against their
saved digests and re-scanned, and a failing security scan blocks whoever asks.

Every field of a row but a structural allowlist -- reason, description, decision note,
review-check and security-issue messages, anything added later -- is secret-redacted on the way
out, as file bodies are: this is the read scope, and an agent writes some of those fields.

Every refusal is a `SkillLibraryErrorOut` with the library's stable `code`
(`_skill_library_http.STATUS` maps it).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from felix.auth.mgmt import SCOPE_SKILLS_READ, SCOPE_SKILLS_WRITE, subject_from_request
from felix.skills import library
from felix.skills.format import bundle_path_issue
from felix.skills.library_store import ANY_LIVE, MAX_VERSIONS_LISTED, DraftCursor, ExpectedLive
from felix.skills.policy import delete_publish_policy, load_publish_policy, policy_body, set_publish_policy
from felix.skills.upstream import recorded_state

from felix_api.routes._skill_library_http import (
    ERRORS,
    LibraryRequest,
    addressable,
    bad_cursor,
    error,
    library_request,
    not_found,
    refusal,
    written_version,
)
from felix_api.routes._skill_library_models import (
    AdoptIn,
    BundleIn,
    CreateSkillIn,
    MakeLiveIn,
    NewVersionIn,
    PolicyPatchIn,
    RejectIn,
    ReviewQueueOut,
    SkillArchivedOut,
    SkillDetailOut,
    SkillFileOut,
    SkillListOut,
    SkillPolicyOut,
    SkillPreviewOut,
    SkillVersionDetailOut,
    SkillVersionOut,
    SkillWriteOut,
)

router = APIRouter()

# Probes one listing runs at once (`shadows_operator_upload` is one object-store HEAD per key).
_PROBE_CONCURRENCY = 16


async def _version_detail(ctx: LibraryRequest, row: dict[str, Any]) -> dict[str, Any]:
    name, version = str(row["name"]), str(row["version"])
    files = await ctx.lib.list_files(ctx.tenant_id, name, version)
    shadows = await ctx.shadows(name, [version])
    return {**ctx.redact(row), "files": files, "shadows_operator_upload": shadows}


def _encode_review_cursor(row: dict[str, Any]) -> str:
    # Neither a skill name nor a version holds a colon, so the split back is unambiguous.
    return f"{row['created_at']}:{row['name']}:{row['version']}"


def _decode_review_cursor(cursor: str) -> DraftCursor | None:
    """The `(created_at, name, version)` a review-queue cursor names, or None when it is not
    one this route issued. ASCII digits only: `str.isdigit` accepts `²`, which `int` refuses."""
    at, _, rest = cursor.partition(":")
    name, _, version = rest.partition(":")
    if not at or not at.isascii() or not at.isdigit() or not name or not version:
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


@router.get("", response_model=SkillListOut, responses=ERRORS)
async def list_library(
    request: Request,
    status: Literal["live", "draft", "archived"] | None = Query(
        default=None,
        description="`live`: has a live version. `draft`: has drafts awaiting review. `archived`: neither.",
    ),
    source: Literal["agent", "operator", "import"] | None = Query(
        default=None, description="Who wrote the newest version (`import`: fetched from GitHub)."
    ),
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=64),
) -> Any:
    """The tenant's library skills by name, each with its live and newest version.

    Pages by name. The filters apply to a page after it is read, so a filtered page can be
    short -- even empty -- and still have a next one; keep following `next_cursor` until null.
    """
    ctx = library_request(request, SCOPE_SKILLS_READ)
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


@router.get("/-/review", response_model=ReviewQueueOut, responses=ERRORS)
async def review_queue(
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=128),
) -> Any:
    """Every draft in the tenant awaiting a decision, oldest first, across skills."""
    ctx = library_request(request, SCOPE_SKILLS_READ)
    after = _decode_review_cursor(cursor) if cursor else None
    if cursor and after is None:
        return bad_cursor()
    drafts = await ctx.lib.list_drafts(ctx.tenant_id, limit=limit, after=after)
    skills = await ctx.lib.get_skills(ctx.tenant_id, {str(d["name"]) for d in drafts})
    items = [
        {**ctx.redact(d), "live_version": (skills.get(str(d["name"])) or {}).get("live_version")}
        for d in drafts
    ]
    next_cursor = _encode_review_cursor(drafts[-1]) if len(drafts) == limit else None
    return {"items": items, "next_cursor": next_cursor}


@router.get("/-/policy", response_model=SkillPolicyOut, responses=ERRORS)
async def get_publish_policy(request: Request) -> Any:
    """The gate every publish and rollback passes: the deployment's `FELIX_SKILL_PUBLISH_*`
    settings, tightened by the tenant's own policy when it has one. `source` says which."""
    ctx = library_request(request, SCOPE_SKILLS_READ)
    return policy_body(await load_publish_policy(ctx.settings, ctx.tenant_id))


@router.patch("/-/policy", response_model=SkillPolicyOut, responses=ERRORS)
async def patch_publish_policy(body: PolicyPatchIn, request: Request) -> Any:
    """Set fields of the tenant's own publish policy; returns the policy now in force.

    Only the fields sent change. Any value in range is stored, but the policy in force is the
    deployment's settings tightened by it: a value looser than a setting is outvoted, and
    `source` is then `tenant+settings`. A failing security scan blocks whatever this says.
    """
    ctx = library_request(request, SCOPE_SKILLS_WRITE)
    changes = body.model_dump(include=body.model_fields_set)
    state = await set_publish_policy(ctx.settings, ctx.tenant_id, changes, by=subject_from_request(request))
    return policy_body(state)


@router.delete("/-/policy", response_model=SkillPolicyOut, responses=ERRORS)
async def delete_publish_policy_route(request: Request) -> Any:
    """Drop the tenant's own policy, so the settings alone decide; returns the policy now in force."""
    ctx = library_request(request, SCOPE_SKILLS_WRITE)
    return policy_body(
        await delete_publish_policy(ctx.settings, ctx.tenant_id, by=subject_from_request(request))
    )


@router.get("/{name}", response_model=SkillDetailOut, responses=ERRORS)
async def get_library_skill(name: str, request: Request) -> Any:
    """One skill and every version it holds (metadata only; files are read per version)."""
    ctx = library_request(request, SCOPE_SKILLS_READ)
    if not addressable(name):
        return not_found(name)
    skill = await ctx.lib.get_skill(ctx.tenant_id, name)
    if skill is None:
        return not_found(name)
    versions = await ctx.lib.list_versions(ctx.tenant_id, name, limit=MAX_VERSIONS_LISTED)
    newest = versions[0]["version"] if versions else None
    shadows = await ctx.shadows(name, [skill["live_version"], newest])
    return {
        **skill,
        "versions": [ctx.redact(v) for v in versions],
        "shadows_operator_upload": shadows,
        "upstream": await recorded_state(ctx.settings, ctx.tenant_id, name, now=library.now_ms()),
    }


@router.get("/{name}/versions/{version}", response_model=SkillVersionDetailOut, responses=ERRORS)
async def get_library_version(name: str, version: str, request: Request) -> Any:
    """One version: its review record, the checks and scan findings it was saved with, and
    its files' digests."""
    ctx = library_request(request, SCOPE_SKILLS_READ)
    if not addressable(name, version):
        return not_found(f"{name}@{version}")
    row = await ctx.lib.get_version(ctx.tenant_id, name, version)
    if row is None:
        return not_found(f"{name}@{version}")
    return await _version_detail(ctx, row)


def _text_type(path: str) -> str:
    if path.endswith(".md"):
        return "text/markdown; charset=utf-8"
    if path.endswith(".json"):
        return "application/json"
    return "text/plain; charset=utf-8"


@router.get("/{name}/versions/{version}/files/{path:path}", response_model=SkillFileOut, responses=ERRORS)
async def get_library_file(name: str, version: str, path: str, request: Request) -> Any:
    """One file of a version, checked against the digest recorded when it was saved.

    Text is secret-redacted, as `GET /skills/{manifest}/{skill}` redacts a body: this is the
    lower scope, and an embedded credential must not ride out on it. A binary asset comes back
    base64 and unredacted -- there is no text in it to redact.
    """
    from felix.secrets import redact_text
    from felix.skills.binary import binary_asset_mime_type, is_binary_asset_path

    ctx = library_request(request, SCOPE_SKILLS_READ)
    if not addressable(name, version):
        return not_found(f"{name}@{version}")
    if path != "SKILL.md" and bundle_path_issue(path) is not None:
        return error(422, "invalid_path", "not a bundle file path")
    files = await ctx.lib.list_files(ctx.tenant_id, name, version)
    meta = next((f for f in files if f["path"] == path), None)
    if meta is None:
        return not_found(f"{name}@{version}/{path}")
    try:
        content = await library.read_version_file(
            ctx.settings, ctx.tenant_id, name, version, path, object_store=ctx.store
        )
    except library.SkillLibraryError as exc:
        return refusal(exc)
    if content is None:
        return not_found(f"{name}@{version}/{path}")
    binary = is_binary_asset_path(path)
    return {
        "path": path,
        "encoding": "base64" if binary else "utf-8",
        "content_type": binary_asset_mime_type(path) if binary else _text_type(path),
        "content": content if binary else redact_text(content, ctx.secrets),
        "sha256": meta["sha256"],
        "size": meta["size"],
    }


@router.get("/{name}/versions/{version}/preview", response_model=SkillPreviewOut, responses=ERRORS)
async def preview_library_version(name: str, version: str, request: Request) -> Any:
    """Re-run review and the security scan on the stored bytes and say whether the publish
    policy would pass them. Read-only: no state changes and nothing is audited."""
    ctx = library_request(request, SCOPE_SKILLS_READ)
    if not addressable(name, version):
        return not_found(f"{name}@{version}")
    row = await ctx.lib.get_version(ctx.tenant_id, name, version)
    if row is None:
        return not_found(f"{name}@{version}")
    verdict = await library.evaluate_version(
        ctx.settings,
        ctx.tenant_id,
        name,
        version,
        policy=(await load_publish_policy(ctx.settings, ctx.tenant_id)).policy,
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


async def _saved(
    request: Request, ctx: LibraryRequest, body: BundleIn, **save: Any
) -> JSONResponse | dict[str, Any]:
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
        return refusal(exc)
    return await written_version(ctx, by, saved, publish=body.publish)


@router.post("", status_code=201, response_model=SkillWriteOut, responses=ERRORS)
async def create_library_skill(body: CreateSkillIn, request: Request) -> Any:
    """Save a new skill as an operator draft (version 0.1.0), and publish it if `publish`.

    The name is the SKILL.md's own. 409 `skill_exists` if the library already holds it --
    `PUT /{name}/versions` adds a version.
    """
    ctx = library_request(request, SCOPE_SKILLS_WRITE)
    return await _saved(request, ctx, body, expect_newest=library.MUST_NOT_EXIST)


@router.put("/{name}/versions", status_code=201, response_model=SkillWriteOut, responses=ERRORS)
async def save_library_version(name: str, body: NewVersionIn, request: Request) -> Any:
    """Save a new version of an existing skill, edited from `parent_version`.

    `parent_version` must be the skill's newest version (409 `parent_changed` otherwise). The
    new version is `version` if given -- it must be newer than every existing one -- or the
    newest bumped by `bump` (default patch).
    """
    ctx = library_request(request, SCOPE_SKILLS_WRITE)
    if not addressable(name) or await ctx.lib.get_skill(ctx.tenant_id, name) is None:
        return not_found(name)
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


def _expected(body: MakeLiveIn | None) -> ExpectedLive:
    """The live version the caller expects, or `ANY_LIVE` when it said nothing. An explicit
    null is "nothing was live", which is not the same as not saying."""
    if body is None or "expected_live_version" not in body.model_fields_set:
        return ANY_LIVE
    return body.expected_live_version


async def _transition(
    request: Request,
    name: str,
    version: str,
    move: Callable[[LibraryRequest, str], Awaitable[dict[str, Any]]],
) -> Any:
    """Run one state change on ``name@version`` as the caller, mapping a refusal."""
    ctx = library_request(request, SCOPE_SKILLS_WRITE)
    if not addressable(name, version):
        return not_found(f"{name}@{version}")
    try:
        return ctx.redact(await move(ctx, subject_from_request(request)))
    except library.SkillLibraryError as exc:
        return refusal(exc)


@router.post("/{name}/versions/{version}/publish", response_model=SkillVersionOut, responses=ERRORS)
async def publish_library_version(
    name: str, version: str, request: Request, body: MakeLiveIn | None = None
) -> Any:
    """Publish a draft: it becomes the live version, and the version it replaces is archived.
    422 `publish_blocked` with the gate's reasons when the policy refuses it; 409 `live_changed`
    when `expected_live_version` was sent and another version is live."""

    async def move(ctx: LibraryRequest, by: str) -> dict[str, Any]:
        return await library.publish(
            ctx.settings,
            ctx.tenant_id,
            name,
            version,
            by=by,
            object_store=ctx.store,
            expected_live=_expected(body),
        )

    return await _transition(request, name, version, move)


@router.post("/{name}/versions/{version}/rollback", response_model=SkillVersionOut, responses=ERRORS)
async def rollback_library_version(
    name: str, version: str, request: Request, body: MakeLiveIn | None = None
) -> Any:
    """Make a version that was once live, live again -- through the same gate as a publish,
    without its evaluation requirement. 409 `live_changed` as for a publish."""

    async def move(ctx: LibraryRequest, by: str) -> dict[str, Any]:
        return await library.rollback(
            ctx.settings,
            ctx.tenant_id,
            name,
            version,
            by=by,
            object_store=ctx.store,
            expected_live=_expected(body),
        )

    return await _transition(request, name, version, move)


@router.post("/{name}/versions/{version}/reject", response_model=SkillVersionOut, responses=ERRORS)
async def reject_library_version(name: str, version: str, body: RejectIn, request: Request) -> Any:
    """Archive a draft without publishing it, recording the note."""

    async def move(ctx: LibraryRequest, by: str) -> dict[str, Any]:
        return await library.reject(ctx.settings, ctx.tenant_id, name, version, by=by, note=body.note)

    return await _transition(request, name, version, move)


@router.post(
    "/{name}/versions/{version}/adopt", status_code=201, response_model=SkillWriteOut, responses=ERRORS
)
async def adopt_library_version(name: str, version: str, body: AdoptIn, request: Request) -> Any:
    """Vouch for an imported version: save its files, byte for byte, as a new operator draft built
    on it that no longer carries `lineage_import`, recording who adopted it, why, and from which
    version (`adopted_from`). The versions before it keep their mark. The new version is a draft:
    publish it through the ordinary gate, which now judges it as an operator's. Adopt never
    publishes, and no agent tool reaches it.

    409 `not_imported` for a version with no imported text, `parent_rejected` for a rejected
    draft, `parent_changed` unless `version` is the newest version that was not rejected; 422
    `reason_required` for a blank reason. Audited as `skill_adopted`.
    """
    ctx = library_request(request, SCOPE_SKILLS_WRITE)
    if not addressable(name, version):
        return not_found(f"{name}@{version}")
    by = subject_from_request(request)
    try:
        saved = await library.adopt(
            ctx.settings, ctx.tenant_id, name, version, by=by, reason=body.reason, object_store=ctx.store
        )
    except library.SkillLibraryError as exc:
        return refusal(exc)
    return await written_version(ctx, by, saved, publish=False)


@router.delete("/{name}", response_model=SkillArchivedOut, responses=ERRORS)
async def archive_library_skill(name: str, request: Request) -> Any:
    """Take a skill out of every catalog. Its versions and their bytes are kept, and a
    rollback brings one back."""
    ctx = library_request(request, SCOPE_SKILLS_WRITE)
    if not addressable(name):
        return not_found(name)
    try:
        return await library.archive_skill(
            ctx.settings, ctx.tenant_id, name, by=subject_from_request(request)
        )
    except library.SkillLibraryError as exc:
        return refusal(exc)
