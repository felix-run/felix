"""The skill library's quality loop, for an operator: feedback and evaluations.

Mounted under `/skill-library` beside `skill_library.py`, sharing its request context, refusal
mapping and redaction (`_skill_library_http.py`), so a refusal here reads like one there and every
text field an agent or a model wrote -- a feedback body, a suggested patch, an error, a scenario,
a judge's reason -- is secret-redacted on the way out.

- **Feedback.** Anyone holding `skills:write` files it (`source: human`); an agent files it with
  `submit_skill_feedback`. A person accepts or rejects it. An accept with `improve` (the default)
  queues an AI rewrite that the worker runs into a *draft* in the review queue -- an agent's
  feedback never starts one on its own, and nothing here publishes.
- **Evaluations.** Queued here, run by the worker: each scenario answered with and without the
  skill, both answers scored 0-100 by the judge, uplift the difference. A version holds one
  queued or running evaluation at a time. Each evaluation says whether it `counts_for_gate`:
  for an agent's version only one on the bundle's own scenarios does.

Accepting with `improve` and queueing an evaluation are jobs, bounded per tenant (429
`skill_jobs_cap_reached`). The worker's `skill_jobs` sweep runs them within a minute; there is no
faster path from the API, which never enqueues to the worker directly.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Query, Request
from felix.auth.mgmt import subject_from_request
from felix.skills import evaluate, feedback, library
from felix.skills.eval_store import get_skill_eval_store
from felix.skills.feedback_store import get_skill_feedback_store
from felix.skills.publish_gate import eval_counts_for_gate, gate_source
from felix.skills.quality_store import FeedbackStatus

from felix_api.routes._skill_library_http import (
    ERRORS,
    ROW_ID_RE,
    LibraryRequest,
    addressable,
    bad_cursor,
    decode_row_cursor,
    library_request,
    not_found,
    refusal,
    row_page,
)
from felix_api.routes._skill_library_models import (
    AcceptFeedbackIn,
    FeedbackIn,
    RejectIn,
    SkillEvalListOut,
    SkillEvalOut,
    SkillFeedbackListOut,
    SkillFeedbackOut,
)

router = APIRouter()


# -- feedback --------------------------------------------------------------------------------

# Declared before `/{name}/feedback`, which would otherwise take `/-/feedback` with name `-`.


@router.get("/-/feedback", response_model=SkillFeedbackListOut, responses=ERRORS)
async def feedback_inbox(
    request: Request,
    status: FeedbackStatus = Query(default="pending"),
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=96),
) -> Any:
    """Feedback across every skill in one status (`pending` by default), oldest first: the one
    that has waited longest is the one to read next."""
    ctx = await library_request(request, "read")
    after = decode_row_cursor(cursor) if cursor else None
    if cursor and after is None:
        return bad_cursor()
    rows = await get_skill_feedback_store(ctx.settings).list_by_status(
        ctx.tenant_id, status, limit=limit, after=after
    )
    return row_page(ctx, rows, limit)


Decide = Callable[[LibraryRequest, str], Awaitable[dict[str, Any]]]


async def _decision(request: Request, feedback_id: str, decide: Decide) -> Any:
    ctx = await library_request(request, "write")
    if not ROW_ID_RE.match(feedback_id):
        return not_found(f"feedback {feedback_id}")
    try:
        return ctx.redact(await decide(ctx, subject_from_request(request)))
    except library.SkillLibraryError as exc:
        return refusal(exc)


@router.post("/-/feedback/{feedback_id}/accept", response_model=SkillFeedbackOut, responses=ERRORS)
async def accept_feedback(feedback_id: str, request: Request, body: AcceptFeedbackIn | None = None) -> Any:
    """Accept pending feedback. With `improve` (the default) the worker rewrites the skill from
    it into a draft for review; the feedback moves to `applied` with `result_version`, or to
    `failed` with `error`. 409 `feedback_conflict` when it was already decided; 429
    `skill_jobs_cap_reached` when `improve` would pass the tenant's job caps."""
    choice = body or AcceptFeedbackIn()

    async def decide(ctx: LibraryRequest, by: str) -> dict[str, Any]:
        return await feedback.accept_feedback(
            ctx.settings, ctx.tenant_id, feedback_id, by=by, improve=choice.improve, note=choice.note
        )

    return await _decision(request, feedback_id, decide)


@router.post("/-/feedback/{feedback_id}/reject", response_model=SkillFeedbackOut, responses=ERRORS)
async def reject_feedback(feedback_id: str, body: RejectIn, request: Request) -> Any:
    """Reject pending feedback, recording the note. Nothing runs."""

    async def decide(ctx: LibraryRequest, by: str) -> dict[str, Any]:
        return await feedback.reject_feedback(ctx.settings, ctx.tenant_id, feedback_id, by=by, note=body.note)

    return await _decision(request, feedback_id, decide)


@router.post("/{name}/feedback", status_code=201, response_model=SkillFeedbackOut, responses=ERRORS)
async def submit_feedback(name: str, body: FeedbackIn, request: Request) -> Any:
    """File feedback on a skill as the caller (`source: human`), on `target_version` or the live
    version. It waits as `pending` until someone accepts or rejects it."""
    ctx = await library_request(request, "write")
    if not addressable(name):
        return not_found(name)
    by = subject_from_request(request)
    try:
        row = await feedback.submit_feedback(
            ctx.settings,
            ctx.tenant_id,
            name=name,
            body=body.body,
            provenance=feedback.FeedbackProvenance(source="human", author=by, principal=by),
            suggested_patch=body.suggested_patch,
            target_version=body.target_version,
        )
    except library.SkillLibraryError as exc:
        return refusal(exc)
    return ctx.redact(row)


@router.get("/{name}/feedback", response_model=SkillFeedbackListOut, responses=ERRORS)
async def list_skill_feedback(
    name: str,
    request: Request,
    status: FeedbackStatus | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=96),
) -> Any:
    """One skill's feedback, newest first, optionally in one status."""
    ctx = await library_request(request, "read")
    if not addressable(name) or await ctx.lib.get_skill(ctx.tenant_id, name) is None:
        return not_found(name)
    before = decode_row_cursor(cursor) if cursor else None
    if cursor and before is None:
        return bad_cursor()
    rows = await get_skill_feedback_store(ctx.settings).list_for_skill(
        ctx.tenant_id, name, status=status, limit=limit, before=before
    )
    return row_page(ctx, rows, limit)


# -- evaluations -----------------------------------------------------------------------------


async def _with_gate_standing(ctx: LibraryRequest, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each evaluation with `counts_for_gate` and `gate_note`, decided by the rule the publish
    gate applies (`publish_gate.eval_counts_for_gate`) against the source of its version."""
    sources: dict[tuple[str, str], str | None] = {}
    for row in rows:
        key = (str(row["name"]), str(row["version"]))
        if key not in sources:
            version = await ctx.lib.get_version(ctx.tenant_id, *key)
            sources[key] = gate_source(version)
    out = []
    for row in rows:
        counts, note = eval_counts_for_gate(sources[(str(row["name"]), str(row["version"]))], row)
        out.append({**row, "counts_for_gate": counts, "gate_note": note})
    return out


@router.post(
    "/{name}/versions/{version}/eval", status_code=202, response_model=SkillEvalOut, responses=ERRORS
)
async def queue_skill_eval(name: str, version: str, request: Request) -> Any:
    """Queue an evaluation of a version; the worker runs it within a minute. Poll
    `GET /{name}/evals/{id}`. 409 `eval_in_progress` while one is queued or running; 429
    `skill_jobs_cap_reached` past the tenant's job caps."""
    ctx = await library_request(request, "write")
    if not addressable(name, version):
        return not_found(f"{name}@{version}")
    try:
        row = await evaluate.queue_eval(
            ctx.settings, ctx.tenant_id, name, version, requested_by=subject_from_request(request)
        )
    except library.SkillLibraryError as exc:
        return refusal(exc)
    (shown,) = await _with_gate_standing(ctx, [row])
    return ctx.redact(shown)


@router.get("/{name}/evals", response_model=SkillEvalListOut, responses=ERRORS)
async def list_skill_evals(
    name: str,
    request: Request,
    version: str | None = Query(default=None, max_length=32),
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=96),
) -> Any:
    """One skill's evaluations, newest first, optionally of one version."""
    ctx = await library_request(request, "read")
    if not addressable(name, version) or await ctx.lib.get_skill(ctx.tenant_id, name) is None:
        return not_found(name)
    before = decode_row_cursor(cursor) if cursor else None
    if cursor and before is None:
        return bad_cursor()
    rows = await get_skill_eval_store(ctx.settings).list_for_skill(
        ctx.tenant_id, name, version=version, limit=limit, before=before
    )
    return row_page(ctx, await _with_gate_standing(ctx, rows), limit)


@router.get("/{name}/evals/{eval_id}", response_model=SkillEvalOut, responses=ERRORS)
async def get_skill_eval(name: str, eval_id: str, request: Request) -> Any:
    """One evaluation, with each scenario's prompt, both scores and the judge's reasons."""
    ctx = await library_request(request, "read")
    if not addressable(name) or not ROW_ID_RE.match(eval_id):
        return not_found(f"evaluation {eval_id}")
    row = await get_skill_eval_store(ctx.settings).get(ctx.tenant_id, eval_id)
    if row is None or row["name"] != name:
        return not_found(f"evaluation {eval_id}")
    (shown,) = await _with_gate_standing(ctx, [row])
    return ctx.redact(shown)
