"""Bring skills published on GitHub into the tenant library: browse a repository, import one.

Mounted under `/skill-library`, beside `skill_library.py`, at collection-wide `/-/` paths. An
import is a draft like any other save -- it waits in the review queue -- and `publish: true` runs
the same gate `POST .../publish` does, which holds an imported version to a stricter bar
(`publish_gate.policy_for_source`). The fetch is `skills/importer.py`: pinned to one commit, to
`api.github.com` only, through the egress guard.

Browsing reads with `skills:read`, as every library read does; importing changes the library and
needs `skills:write`. `FELIX_SKILL_IMPORT_SOURCES` bounds both, so a reader cannot use the
server's GitHub token to list a repository the deployment never meant to reach.

Refusals are `SkillLibraryErrorOut` with a stable code (`_skill_library_http.STATUS`): a source
that can never be fetched is 422, one the allowlist refuses 403, one GitHub does not have 404, a
name the library holds from another origin 409 `origin_mismatch`, and GitHub itself failing --
rate limited, unreachable, answering in error -- 502.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request, Response
from felix.auth.mgmt import SCOPE_SKILLS_READ, SCOPE_SKILLS_WRITE, subject_from_request
from felix.skills import importer, library

from felix_api.routes._skill_library_http import IMPORT_ERRORS, library_request, refusal, written_version
from felix_api.routes._skill_library_models import ImportIn, SkillBrowseOut, SkillImportOut

router = APIRouter()


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
    name and description its frontmatter declares. Each item's `source` is what to import it by."""
    ctx = library_request(request, SCOPE_SKILLS_READ)
    try:
        listing = await importer.browse(ctx.settings, ctx.tenant_id, source, ref)
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
    and the existing version, saving nothing (and publishing nothing). A name the library holds
    from anywhere else is 409 `origin_mismatch`. `dropped_files` lists what the import left out.
    """
    ctx = library_request(request, SCOPE_SKILLS_WRITE)
    by = subject_from_request(request)
    try:
        result = await importer.import_skill(
            ctx.settings, ctx.tenant_id, source=body.source, ref=body.ref, by=by, object_store=ctx.store
        )
    except library.SkillLibraryError as exc:
        return refusal(exc)
    if result.unchanged:
        response.status_code = 200
    written = await written_version(ctx, by, result.version, publish=body.publish and not result.unchanged)
    return {**written, "unchanged": result.unchanged, "dropped_files": result.dropped_files}
