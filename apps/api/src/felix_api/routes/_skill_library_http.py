"""What `skill_library.py` and `skill_quality.py` share. No routes.

One request's view of the library (`library_request`: the caller's tenant, the stores, the secret
values responses are redacted against), how a library refusal becomes a response, and the cursor
both listings page with -- so a refusal, a redaction and a page read the same under either module.

**Redaction is on by default.** `LibraryRequest.redact` secret-redacts every text, list and
object field of a row except a fixed structural allowlist (`STRUCTURAL_FIELDS`: ids, names,
versions, statuses, sources, paths, digests). A field added to a row later is redacted unless
someone decides it is structural; the opposite default is how a new free-text field leaks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fastapi import Request
from fastapi.responses import JSONResponse
from felix.auth.mgmt import require_mgmt_scopes, tenant_id_from_request
from felix.skills import library
from felix.skills.format import is_valid_skill_name
from felix.skills.library_store import SkillLibraryStore, get_skill_library_store
from felix.skills.quality_store import Cursor

from felix_api.routes._skill_library_models import BundleIssueOut, SkillLibraryErrorOut

if TYPE_CHECKING:  # imports stay lazy at runtime; the annotations are the point
    from felix.config import Settings

# `SkillLibraryError.code` → status. 409 is "the library's state disagrees with the request";
# 422 is "this content can never be accepted as sent"; 429 is "wait, and it can succeed".
STATUS: dict[str, int] = {
    "invalid_bundle": 422,
    # The name is the host's: no edit of the bundle makes it the library's.
    "name_shadows_host_skill": 409,
    "not_found": 404,
    "skill_exists": 409,
    "version_conflict": 409,
    "parent_changed": 409,
    # An agent's edit named a rejected draft as its parent; the newest version that was not
    # rejected is the one to edit.
    "parent_rejected": 409,
    # A publish or rollback that named the live version it expected, and another is live:
    # someone else moved it. Reload, then decide again.
    "live_changed": 409,
    # The queue drains as drafts are decided, so a retry later can succeed. Only agent drafts
    # are capped; an operator save through these routes never is.
    "pending_cap_reached": 429,
    # Versions are never deleted, so waiting does not help: the state is the obstacle.
    "version_cap_reached": 409,
    "publish_blocked": 422,
    # Feedback already decided; the inbox moved on underneath the caller.
    "feedback_conflict": 409,
    # An agent's undecided feedback; drains as a person decides it, so a retry later can succeed.
    "feedback_cap_reached": 429,
    # One evaluation in flight per version: wait for it, then queue another.
    "eval_in_progress": 409,
    # The tenant's skill jobs queued or created today (`job_limits`); drains as they run.
    "skill_jobs_cap_reached": 429,
    # The object store no longer holds the bytes the row recorded. Nothing the caller sends
    # fixes that and a retry does not either: a server-side integrity failure, so a 5xx that
    # pages someone, under a code that says which.
    "version_corrupt": 500,
    # An import that names a skill the library holds from another origin, or one an agent or an
    # operator wrote: the import never takes over a name.
    "origin_mismatch": 409,
    # Imports (`skills/importer.py`). The source as written can never be fetched...
    "invalid_source": 422,
    "source_too_large": 422,
    # ...the deployment's FELIX_SKILL_IMPORT_SOURCES does not cover it, or its folder changed
    # within the minimum import age (a policy refusal like the allowlist's; waiting lifts it)...
    "source_not_allowed": 403,
    "too_recent": 403,
    # ...GitHub has no such repository, ref, path or SKILL.md (or it is private and unread)...
    "source_not_found": 404,
    # ...or GitHub could not be asked: rate limited, unreachable, or answering in error. Upstream
    # failures, so a 502 under a code that says which.
    "upstream_rate_limited": 502,
    "upstream_error": 502,
    "egress_blocked": 502,
}

# GitHub failing is a status only the routes that reach GitHub (`skill_import.py`) answer with.
_UPSTREAM_STATUSES = frozenset({502})
ERRORS: dict[int | str, dict[str, Any]] = {
    status: {"model": SkillLibraryErrorOut} for status in sorted({*STATUS.values(), 422} - _UPSTREAM_STATUSES)
}
IMPORT_ERRORS: dict[int | str, dict[str, Any]] = {
    status: {"model": SkillLibraryErrorOut} for status in sorted({*STATUS.values(), 422})
}

# What `feedback.submit_feedback` and `evaluate.queue_eval` mint: a UUID. Anything else is a 404
# before a store is asked.
ROW_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")

# Row fields that are never redacted: identifiers, names, versions, statuses, sources, paths and
# digests -- values the library or the auth layer minted or validated, never free text a saver,
# an agent or a model wrote. Numbers and booleans are never redacted either way.
STRUCTURAL_FIELDS = frozenset(
    {
        "id",
        "tenant_id",
        "name",
        "version",
        "parent_version",
        "live_version",
        "target_version",
        "result_version",
        "status",
        "source",
        "scenario_source",
        "security_status",
        "origin_manifest_id",
        "session_id",
        "author",
        "principal",
        "requested_by",
        "decided_by",
        "created_by",
        "updated_by",
        "model",
        "judge_model",
        "path",
        "sha256",
        "encoding",
        "content_type",
        "files",
        "latest",
        # An import's origin: validated by `skills/github.py` (the source and ref) or minted by git
        # (the commit and the tree digest). The license is GitHub's text, and is redacted.
        "origin_source",
        "origin_ref",
        "origin_commit",
        "origin_tree_hash",
    }
)


def error_body(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return SkillLibraryErrorOut(error=code, message=message, **extra).model_dump(exclude_none=True)


def error(status: int, code: str, message: str, **extra: Any) -> JSONResponse:
    return JSONResponse(error_body(code, message, **extra), status_code=status)


def refusal_body(exc: library.SkillLibraryError) -> dict[str, Any]:
    extra: dict[str, Any] = {}
    if isinstance(exc, library.SkillBundleInvalid):
        extra["issues"] = [BundleIssueOut(path=i.path, message=i.message) for i in exc.issues[:50]]
    if isinstance(exc, library.SkillPublishBlocked):
        extra["reasons"] = list(exc.reasons)
    return error_body(exc.code, str(exc), **extra)


def refusal(exc: library.SkillLibraryError) -> JSONResponse:
    # An unmapped code is a library refusal this module was never taught: a server bug, so 500
    # rather than a guess that reads to a client as "your request conflicted".
    return JSONResponse(refusal_body(exc), status_code=STATUS.get(exc.code, 500))


def not_found(what: str) -> JSONResponse:
    return error(404, "not_found", f"{what} is not in the library")


def bad_cursor() -> JSONResponse:
    return error(422, "invalid_cursor", "cursor is not one this route issued; start from the first page")


def addressable(name: str, version: str | None = None) -> bool:
    """A name and version that could be in the library at all. Anything else is a 404 before
    a store is asked -- and before a path segment reaches an object key."""
    if not is_valid_skill_name(name):
        return False
    return version is None or bool(library.VERSION_RE.match(version))


@dataclass(slots=True)
class LibraryRequest:
    """What every handler needs, resolved once per request: settings, the caller's tenant, the
    stores, and the secret values responses are redacted against."""

    settings: Settings
    tenant_id: str
    lib: SkillLibraryStore
    store: Any
    secrets: list[str]

    def redact(self, row: dict[str, Any]) -> dict[str, Any]:
        """``row`` with every field outside `STRUCTURAL_FIELDS` secret-redacted."""
        from felix.secrets import redact_json, redact_text

        out = dict(row)
        for key, value in out.items():
            if key in STRUCTURAL_FIELDS:
                continue
            if isinstance(value, str):
                out[key] = redact_text(value, self.secrets)
            elif isinstance(value, list | dict):
                out[key] = redact_json(value, self.secrets)
        return out

    async def shadows(self, name: str, versions: list[str | None]) -> bool:
        return await library.shadows_operator_upload(
            self.settings, self.tenant_id, name, versions, object_store=self.store
        )


async def written_version(
    ctx: LibraryRequest, by: str, saved: dict[str, Any], *, publish: bool
) -> dict[str, Any]:
    """The response to a save: the version as stored, its files, and -- with ``publish`` -- the
    outcome of publishing it through the gate. A refused publish leaves the draft saved, and
    ``publish_blocked`` says why it did not also go live."""
    name, version = str(saved["name"]), str(saved["version"])
    published, blocked = False, None
    if publish:
        try:
            await library.publish(ctx.settings, ctx.tenant_id, name, version, by=by, object_store=ctx.store)
            published = True
        except library.SkillLibraryError as exc:
            blocked = refusal_body(exc)
    # Read back, so the response carries every column the store defaults, as a later GET would.
    row = await ctx.lib.get_version(ctx.tenant_id, name, version) or saved
    files = await ctx.lib.list_files(ctx.tenant_id, name, version)
    shadows = saved.get("shadows_operator_upload")
    return {
        **ctx.redact(row),
        "files": files,
        "shadows_operator_upload": await ctx.shadows(name, [version]) if shadows is None else shadows,
        "published": published,
        "publish_blocked": blocked,
    }


def library_request(request: Request, scope: str) -> LibraryRequest:
    """Check ``scope`` and resolve the caller's view of the library."""
    from felix.secrets import collected_secret_values
    from felix.storage import get_object_store

    require_mgmt_scopes(request, scope)
    settings = request.app.state.settings
    return LibraryRequest(
        settings=settings,
        tenant_id=tenant_id_from_request(request),
        lib=get_skill_library_store(settings),
        store=get_object_store(settings),
        secrets=collected_secret_values(settings),
    )


def encode_row_cursor(row: dict[str, Any]) -> str:
    """A feedback or evaluation listing's cursor: `created_at:id`. A UUID holds no colon."""
    return f"{row['created_at']}:{row['id']}"


def decode_row_cursor(cursor: str) -> Cursor | None:
    """The `(created_at, id)` a row cursor names, or None when it is not one a route issued.
    ASCII digits only: `str.isdigit` accepts `²`, which `int` refuses."""
    at, _, row_id = cursor.partition(":")
    if not at or not at.isascii() or not at.isdigit() or not ROW_ID_RE.match(row_id):
        return None
    return int(at), row_id


def row_page(ctx: LibraryRequest, rows: list[dict[str, Any]], limit: int) -> dict[str, Any]:
    return {
        "items": [ctx.redact(r) for r in rows],
        "next_cursor": encode_row_cursor(rows[-1]) if len(rows) == limit else None,
    }


__all__ = [
    "ERRORS",
    "IMPORT_ERRORS",
    "ROW_ID_RE",
    "STATUS",
    "STRUCTURAL_FIELDS",
    "LibraryRequest",
    "addressable",
    "bad_cursor",
    "decode_row_cursor",
    "encode_row_cursor",
    "error",
    "error_body",
    "library_request",
    "not_found",
    "refusal",
    "refusal_body",
    "row_page",
    "written_version",
]
