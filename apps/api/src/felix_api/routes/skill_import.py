"""Bring skills published on GitHub into the tenant library: browse a repository, import one, and
keep it current -- check it against its origin with a diff, and re-import it.

Mounted under `/skill-library`, beside `skill_library.py`, at collection-wide `/-/` paths. An
import is only ever a draft -- it waits in the review queue, and is published by a separate
request after review, through a gate that holds imported text to a stricter bar
(`publish_gate.gate_source`). The fetch is `skills/importer.py`: pinned to one commit of the
repository's own history, to `api.github.com` only, through the egress guard. Every route here is
charged per GitHub call against per-tenant and deployment-wide hourly budgets
(`FELIX_SKILL_IMPORT_CALLS_PER_HOUR[_TOTAL]`, 429 `rate_limited`).

`/{name}/-/upstream` checks an imported skill against its stored origin, with a per-file diff;
`/{name}/-/update` re-imports it as a draft again (`skills/upstream.py`); `/-/upstream` lists every
imported skill's state, checked now or as last recorded. Mounted before `skill_library.py`, so
`/-/upstream` is matched before `/{name}`.

Browsing and checking read with `skills:read`, as every library read does; importing and updating
change the library and need `skills:write`. `FELIX_SKILL_IMPORT_SOURCES` bounds every one per
tenant, so a reader cannot use the server's GitHub token to list a repository the deployment never
bound to its tenant -- an update or a check of a stored origin is judged again, as a ref is.

Refusals are `SkillLibraryErrorOut` with a stable code (`_skill_library_http.STATUS`): a source
that can never be fetched is 422 (as is a commit off the repository's own history and an
ambiguous ref), one the allowlist or the cooldown refuses 403 (`source_not_allowed`, `too_recent`),
one GitHub does not have 404, a name the library holds from another origin 409 `origin_mismatch`
(and a check or update of a skill that was not imported 409 `not_imported`),
a spent call budget 429 `rate_limited`, and GitHub itself failing -- rate limited, unreachable,
answering in error -- 502.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request, Response
from felix.auth.mgmt import subject_from_request
from felix.skills import importer, library, upstream

from felix_api.routes._skill_library_http import (
    IMPORT_ERRORS,
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
    ImportIn,
    SkillBrowseOut,
    SkillImportOut,
    SkillOutdatedOut,
    SkillUpdateOut,
    SkillUpstreamOut,
    UpdateIn,
)

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
    ctx = await library_request(request, "read")
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
    ctx = await library_request(request, "write")
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


# -- updates ---------------------------------------------------------------------------------


@router.get("/-/upstream", response_model=SkillOutdatedOut, responses=IMPORT_ERRORS)
async def list_upstream(
    request: Request,
    refresh: bool = Query(
        default=True,
        description="Check each origin now (GitHub calls, charged); false reads the last recorded checks.",
    ),
    limit: int = Query(default=upstream.MAX_OUTDATED, ge=1, le=upstream.MAX_OUTDATED),
    cursor: str | None = Query(default=None, max_length=64),
) -> Any:
    """The tenant's imported skills, by name, each against its origin: the upstream commit,
    whether its files moved from the newest version's (`update_available`), and when the cooldown
    lets them in. No diffs; `GET /{name}/-/upstream` has one.

    At most 25 a page; follow `next_cursor` until null. With `refresh` each check costs a few
    GitHub calls, charged to the budget, and records what it found; a budget spent part way ends
    the page there (`stopped`), as does running long, and one spent before the first check is 429.
    A refused check is listed with its code (`error`)."""
    ctx = await library_request(request, "read")
    if cursor and not addressable(cursor):
        return bad_cursor()
    try:
        listing = await upstream.outdated(
            ctx.settings,
            ctx.tenant_id,
            after=cursor or None,
            limit=limit,
            refresh=refresh,
            deps=_deps(request, ctx),
        )
    except library.SkillLibraryError as exc:
        return refusal(exc)
    return {**listing, "items": [ctx.redact(i) for i in listing["items"]]}


@router.get("/{name}/-/upstream", response_model=SkillUpstreamOut, responses=IMPORT_ERRORS)
async def check_upstream(
    name: str,
    request: Request,
    ref: str | None = Query(
        default=None,
        min_length=1,
        max_length=200,
        description="Another branch, tag or commit of the skill's source; the stored ref if omitted.",
    ),
) -> Any:
    """An imported skill against its origin now: the commit the ref names, whether the kept files
    moved, when this tenant first saw them and when the cooldown lets them in, and a per-file diff
    against the live version (the newest when nothing is live). Only changed files are fetched.

    Checking records the sighting, as a browse does: asking about an update starts its cooldown.
    The diff is third-party text, redacted as a file read is. 409 `not_imported` for a skill whose
    newest version was not imported; a `ref` passes the same allowlist and rules as an import's.

    The stored ref is a read (`skills:read`). Naming another `ref` needs `skills:write`: it points
    the deployment's token, and the budget, at any branch, tag or commit of the source -- a choice
    of what to fetch, as an import is."""
    ctx = await library_request(request, "write" if ref else "read")
    if not addressable(name):
        return not_found(name)
    try:
        found = await upstream.check_upstream(
            ctx.settings, ctx.tenant_id, name, ref=ref, deps=_deps(request, ctx)
        )
    except library.SkillLibraryError as exc:
        return refusal(exc)
    return ctx.redact(found)


@router.post(
    "/{name}/-/update",
    status_code=201,
    response_model=SkillUpdateOut,
    responses={**IMPORT_ERRORS, 200: {**_UNCHANGED, "model": SkillUpdateOut}},
)
async def update_from_upstream(
    name: str, request: Request, response: Response, body: UpdateIn | None = None
) -> Any:
    """Re-import an imported skill from its stored origin, exactly as `POST /-/import` would: a
    new draft (201), or 200 `unchanged`; `too_recent` inside the cooldown, `origin_mismatch` if the
    name's newest version came from elsewhere. Never published: review the draft, then publish it.

    `diff` is the saved draft against the live version (else the version it was built on). 409
    `not_imported` for a skill whose newest version was not imported."""
    ctx = await library_request(request, "write")
    if not addressable(name):
        return not_found(name)
    by = subject_from_request(request)
    try:
        result, diff = await upstream.update_skill(
            ctx.settings, ctx.tenant_id, name, by=by, ref=body.ref if body else None, deps=_deps(request, ctx)
        )
    except library.SkillLibraryError as exc:
        return refusal(exc)
    if result.unchanged:
        response.status_code = 200
    written = await written_version(ctx, by, result.version, publish=False)
    return {
        **written,
        "unchanged": result.unchanged,
        "dropped_files": result.dropped_files,
        "diff": ctx.redact({"diff": diff})["diff"],
    }
