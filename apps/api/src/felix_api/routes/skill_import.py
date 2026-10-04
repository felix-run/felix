"""Bring skills published on GitHub into the tenant library: browse a repository, import one.

Mounted under `/skill-library`, beside `skill_library.py`, at collection-wide `/-/` paths. An
import is only ever a draft -- it waits in the review queue, and is published by a separate
request after review, through a gate that holds imported text to a stricter bar
(`publish_gate.gate_source`). The fetch is `skills/importer.py`: pinned to one commit of the
repository's own history, to `api.github.com` only, through the egress guard. Both routes are
charged per GitHub call against per-tenant and deployment-wide hourly budgets
(`FELIX_SKILL_IMPORT_CALLS_PER_HOUR[_TOTAL]`, 429 `rate_limited`).

Browsing reads with `skills:read`, as every library read does; importing changes the library and
needs `skills:write`. `FELIX_SKILL_IMPORT_SOURCES` bounds both per tenant, so a reader cannot use
the server's GitHub token to list a repository the deployment never bound to its tenant.

Refusals are `SkillLibraryErrorOut` with a stable code (`_skill_library_http.STATUS`): a source
that can never be fetched is 422 (as is a commit off the repository's own history and an
ambiguous ref), one the allowlist or the cooldown refuses 403 (`source_not_allowed`, `too_recent`),
one GitHub does not have 404, a name the library holds from another origin 409 `origin_mismatch`,
a spent call budget 429 `rate_limited`, and GitHub itself failing -- rate limited, unreachable,
answering in error -- 502.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request, Response
from felix.auth.mgmt import SCOPE_SKILLS_READ, SCOPE_SKILLS_WRITE, subject_from_request
from felix.skills import importer, library

from felix_api.routes._skill_library_http import (
    IMPORT_ERRORS,
    LibraryRequest,
    error,
    library_request,
    refusal,
    written_version,
)
from felix_api.routes._skill_library_models import ImportIn, SkillBrowseOut, SkillImportOut

router = APIRouter()


def _deps(request: Request, ctx: LibraryRequest) -> importer.ImportDeps:
    """The production seams: the request's object store, and a budget charged per GitHub call on
    a limiter store of its own (`app.state.skill_import_limiter`) -- per tenant and for the whole
    deployment, since every call spends the deployment's token and GitHub's limit for it."""
    budget = importer.github_call_budget(request.app.state.skill_import_limiter, ctx.settings, ctx.tenant_id)
    return importer.ImportDeps(object_store=ctx.store, charge=budget)


@router.get("/-/browse", response_model=SkillBrowseOut, responses=IMPORT_ERRORS)
async def browse_source(
    request: Request,
    source: str = Query(min_length=1, max_length=512, description="`github:owner/repo[/path]`."),
    ref: str | None = Query(
        default=None,
        min_length=1,
        max_length=200,
        description="Branch, tag or commit; default branch if omitted.",
    ),
) -> Any:
    """The skills a GitHub repository offers at one commit: every directory with a `SKILL.md`
    under the usual roots (`skills/`, `.claude/skills/`, Claude Code plugin layouts, …), with the
    name and description its frontmatter declares. Each item's `source` is what to import it by,
    and `eligible_at` when the minimum import age lets it in -- counted from the first time this
    tenant saw those files, which a browse records."""
    ctx = library_request(request, SCOPE_SKILLS_READ)
    try:
        listing = await importer.browse(ctx.settings, ctx.tenant_id, source, ref, deps=_deps(request, ctx))
    except library.SkillLibraryError as exc:
        return refusal(exc)
    return ctx.redact(listing)


_UNCHANGED = {
    "model": SkillImportOut,
    "description": "Unchanged: the newest version already holds these files, and nothing was saved.",
}


@router.post(
    "/-/import",
    status_code=201,
    response_model=SkillImportOut,
    responses={**IMPORT_ERRORS, 200: _UNCHANGED},
)
async def import_from_source(body: ImportIn, request: Request, response: Response) -> Any:
    """Fetch the skill at `source`, pinned to the commit `ref` resolves to, and save it as a draft.

    A new skill starts at 0.1.0. A skill the library already imported from the same source gets
    a new version -- unless its files are unchanged, which answers 200 with `unchanged: true`
    and the existing version, saving nothing. A name the library holds from anywhere else is 409
    `origin_mismatch`. `dropped_files` lists what the import left out.

    Never published here: `publish: true` is 422 `publish_not_allowed`. Third-party text is
    reviewed first, then published with `POST /skill-library/{name}/versions/{version}/publish`.
    """
    ctx = library_request(request, SCOPE_SKILLS_WRITE)
    if body.publish:
        return error(
            422,
            "publish_not_allowed",
            "an import is saved as a draft for review; publish it with "
            "POST /skill-library/{name}/versions/{version}/publish once it has been read",
        )
    by = subject_from_request(request)
    try:
        result = await importer.import_skill(
            ctx.settings, ctx.tenant_id, source=body.source, ref=body.ref, by=by, deps=_deps(request, ctx)
        )
    except library.SkillLibraryError as exc:
        return refusal(exc)
    if result.unchanged:
        response.status_code = 200
    written = await written_version(ctx, by, result.version, publish=False)
    return {**written, "unchanged": result.unchanged, "dropped_files": result.dropped_files}
