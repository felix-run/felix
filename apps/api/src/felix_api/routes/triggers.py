"""`POST /triggers/{id}`: a signed event from elsewhere starts a durable run (`felix.triggers`).

No bearer: the sender signs the body, and nothing in it is parsed before the signature verifies.
In order: the body fits; the trigger exists and the signature verifies (one answer for both, so
ids cannot be probed); the body is UTF-8 JSON; then, once per delivery -- a replay gets the first
answer back -- the event type is one the trigger runs for; for a workflow trigger, the payload
supplies the workflow's inputs (else `invalid_payload`, kept as the answer); the trigger's
per-minute limit has room; and the run starts as the trigger's own principal -- a durable chat on
a thread of its own, or a workflow run. Ignored deliveries are
claimed too, so a captured one cannot be relabelled into a run later; only deliveries that would
start a run spend the limit. Every refusal names its kind and nothing else: the reason is logged.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from felix.auth.actors import TRIGGER_RUN_PREFIX
from felix.context import RequestContext, async_run_with_context
from felix.logging_setup import loggable
from felix.triggers import (
    MAX_PAYLOAD_BYTES,
    TRIGGER_EVENT_TOOL,
    Delivery,
    ManifestTarget,
    PayloadMismatch,
    SignedRequest,
    Trigger,
    TriggerRejected,
    WorkflowTarget,
    event_record,
    intake_payload,
    now_s,
    parse_triggers,
    render_prompt,
    resolved_keys,
    verifier_for,
    workflow_inputs,
)
from pydantic import BaseModel

if TYPE_CHECKING:
    from felix.context import AuthContext

logger = logging.getLogger("felix_api.routes.triggers")

router = APIRouter(tags=["Triggers"])

WINDOW_S = 60


class TriggerAnswer(BaseModel):
    """`accepted` with the run's id, or `ignored` for an event type the trigger does not run for."""

    status: str
    run_id: str | None = None


_ERRORS: dict[int | str, dict[str, Any]] = {
    200: {
        "model": TriggerAnswer,
        "description": "The event type is not one this trigger runs for (`ignored`).",
    },
    401: {"description": "No such trigger, or the signature did not verify (`invalid_signature`)."},
    409: {"description": "`delivery_in_progress`, or `delivery_id_reused` for another body."},
    413: {"description": "The body is larger than a trigger accepts (`payload_too_large`)."},
    422: {
        "description": (
            "The body is not UTF-8 JSON, or does not supply the workflow's inputs (`invalid_payload`)."
        )
    },
    429: {"description": "The trigger's per-minute limit is spent (`rate_limited`)."},
    503: {"description": "The trigger is misconfigured or its run could not start (`trigger_unavailable`)."},
}


class _Unavailable(Exception):
    """The trigger cannot start its run; `reason` is for the log."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _RateLimited(Exception):
    """Raised inside the claim, so the claim is released and a later retry can run."""


@router.post("/{trigger_id}", status_code=202, response_model=TriggerAnswer, responses=_ERRORS)
async def receive_trigger(trigger_id: str, request: Request) -> Any:
    from felix.idempotency import IdempotencyConflict, StoredResponse, once

    settings = request.app.state.settings
    body = await request.body()
    if len(body) > MAX_PAYLOAD_BYTES:
        raise HTTPException(status_code=413, detail="payload_too_large")
    trigger = parse_triggers(settings).get(trigger_id)
    try:
        if trigger is None:
            raise TriggerRejected("unknown_trigger")
        delivery = await _verify(settings, trigger, request, body)
    except TriggerRejected as exc:
        logger.info("trigger_rejected trigger=%s reason=%s", loggable(trigger_id, limit=64), exc.code)
        raise HTTPException(status_code=401, detail="invalid_signature") from None
    try:
        payload, quarantined = await intake_payload(body)
    except ValueError:
        raise HTTPException(status_code=422, detail="invalid_payload") from None

    async def handle() -> StoredResponse:
        if not trigger.runs_for(delivery.event):
            return StoredResponse(status=200, body={"status": "ignored"})
        target = trigger.target
        prepared: _PreparedWorkflowRun | None = None
        if isinstance(target, WorkflowTarget):
            # Before the limit, so a payload that starts nothing spends none of it. Refusals here are
            # the sender's payload, not the trigger: told as `invalid_payload`, and kept as the answer
            # to this delivery, so a retry of the same body gets it back.
            if quarantined:
                from felix.observability.metrics import record_counter

                logger.info("trigger_payload_quarantined trigger=%s", trigger.id)
                record_counter(
                    "felix_content_screening",
                    {"manifest_id": "", "tool": TRIGGER_EVENT_TOOL, "action": "quarantine"},
                )
                return StoredResponse(status=422, body={"detail": "invalid_payload"})
            try:
                prepared = await _prepare_workflow_run(settings, trigger, target, delivery, payload)
            except PayloadMismatch as mismatch:
                logger.info(
                    "trigger_payload_mismatch trigger=%s reason=%s",
                    trigger.id,
                    loggable(mismatch.reason, limit=200),
                )
                return StoredResponse(status=422, body={"detail": "invalid_payload"})
        if not await request.app.state.trigger_limiter.hit(
            f"trigger:{trigger.id}", limit=trigger.rate_limit, window_seconds=WINDOW_S
        ):
            raise _RateLimited
        if prepared is not None:
            run_id = await _start_workflow_run(settings, trigger, delivery, prepared)
        else:
            assert isinstance(target, ManifestTarget)
            run_id = await _start_run(settings, trigger, target, delivery, payload, quarantined)
        return StoredResponse(status=202, body={"status": "accepted", "run_id": run_id})

    try:
        response, replayed = await once(
            request.app.state.trigger_claims,
            f"trigger:{trigger.tenant}:{trigger.id}",
            delivery.replay_key,
            hashlib.sha256(body).hexdigest(),
            handle,
        )
    except IdempotencyConflict as exc:
        detail = "delivery_in_progress" if exc.kind == "in_progress" else "delivery_id_reused"
        raise HTTPException(status_code=409, detail=detail) from exc
    except _RateLimited:
        raise HTTPException(status_code=429, detail="rate_limited") from None
    except _Unavailable as exc:
        logger.warning("trigger_unavailable trigger=%s reason=%s", trigger.id, exc.reason)
        raise HTTPException(status_code=503, detail="trigger_unavailable") from None
    headers = {"idempotent-replayed": "true"} if replayed else None
    return JSONResponse(response.body, status_code=response.status, headers=headers)


async def _verify(settings: Any, trigger: Trigger, request: Request, body: bytes) -> Delivery:
    try:
        keys = await resolved_keys(settings, trigger)
    except Exception:
        # An unresolvable or too-short secret: the trigger is broken, not the delivery.
        logger.warning("trigger_unavailable trigger=%s reason=secret", trigger.id, exc_info=True)
        raise HTTPException(status_code=503, detail="trigger_unavailable") from None
    signed = SignedRequest(
        headers=request.headers, body=body, now=now_s(), path=request.url.path, method=request.method
    )
    return verifier_for(trigger.scheme)(keys, signed)


async def _admitted(settings: Any, trigger: Trigger, target: ManifestTarget, thread: str, auth: Any) -> Any:
    """The trigger's manifest, resolved and admitted for its principal -- or `_Unavailable`. Called
    inside the trigger tenant's context, which is what the store's row-level security reads."""
    from felix.runtime import prepare_tenant_invoke, resolve_tenant_manifest

    try:
        resolved = await resolve_tenant_manifest(settings, trigger.tenant, target.manifest, thread_id=thread)
    except Exception as exc:
        raise _Unavailable(f"manifest {target.manifest!r} did not resolve") from exc
    if TRIGGER_EVENT_TOOL not in resolved.manifest.spec.tools:
        # Without it the model has no way to read the event, and a run on the prompt alone is a
        # run that cannot do what it was started for.
        raise _Unavailable(f"manifest {target.manifest!r} does not list {TRIGGER_EVENT_TOOL}")
    try:
        await prepare_tenant_invoke(settings, resolved=resolved, auth=auth, thread_id=thread)
    except Exception as exc:
        raise _Unavailable("the manifest refused the trigger's principal") from exc
    return resolved


def _trigger_auth(trigger: Trigger) -> AuthContext:
    from felix.context import AuthContext

    return AuthContext(
        principal_sub=f"{TRIGGER_RUN_PREFIX}{trigger.id}",
        tenant_id=trigger.tenant,
        scopes=trigger.scopes,
        anonymous=False,
        scheme="trigger",
        # No person is there to answer an approval for a sender's event. Not `scheduled`: that means
        # "descends from a job", which `schedule_task` reads; a trigger run is refused there anyway,
        # as a shared subject.
        unattended=True,
    )


def _trigger_context(settings: Any, trigger: Trigger) -> RequestContext:
    """The trigger's principal in a context of its own, unattended like every trigger run."""
    return RequestContext(settings=settings, auth=_trigger_auth(trigger), unattended=True)


@dataclass(frozen=True, slots=True)
class _PreparedWorkflowRun:
    workflow: dict[str, Any]
    inputs: dict[str, Any]
    untrusted: tuple[str, ...]


async def _prepare_workflow_run(
    settings: Any, trigger: Trigger, target: WorkflowTarget, delivery: Delivery, payload: str
) -> _PreparedWorkflowRun:
    """The trigger's workflow and its inputs from the payload, as the trigger maps them: each
    checked against what the workflow declares (`PayloadMismatch` otherwise), and those that are
    the sender's text listed. `_Unavailable` when the workflow is not there to run."""
    from felix.workflows import store as workflow_store
    from felix.workflows.schema import WorkflowSpec

    async with async_run_with_context(_trigger_context(settings, trigger)):
        workflow = await workflow_store.get_workflow(settings, trigger.tenant, target.workflow)
    if workflow is None:
        raise _Unavailable(f"workflow {target.workflow!r} does not exist")
    try:
        declared = WorkflowSpec.model_validate(workflow["definition"]).inputs
    except ValueError as exc:
        raise _Unavailable(f"workflow {target.workflow!r} does not parse") from exc
    inputs, untrusted = workflow_inputs(trigger, delivery, payload, declared)
    return _PreparedWorkflowRun(workflow=workflow, inputs=inputs, untrusted=untrusted)


async def _start_workflow_run(
    settings: Any, trigger: Trigger, delivery: Delivery, prepared: _PreparedWorkflowRun
) -> str:
    """The run, as `trigger:<id>` with the trigger's scopes, held to the trigger's `revision` if
    it pins one; its id. Every agent step of it fences what it is given (`runs.fences_prompts`)."""
    from felix.workflows import engine

    assert isinstance(trigger.target, WorkflowTarget)
    who = f"{TRIGGER_RUN_PREFIX}{trigger.id}"
    async with async_run_with_context(_trigger_context(settings, trigger)):
        try:
            run = await engine.start_run(
                settings,
                trigger.tenant,
                prepared.workflow,
                prepared.inputs,
                trigger=who,
                started_by=who,
                revision=trigger.target.revision,
                untrusted_inputs=prepared.untrusted,
            )
        except engine.StartRefused as refused:
            # Not the sender's doing: the trigger or its workflow needs the operator.
            raise _Unavailable(f"workflow {trigger.target.workflow!r}: {refused.message}") from None
    logger.info(
        "trigger_started trigger=%s event=%s workflow_run=%s",
        trigger.id,
        loggable(delivery.event, limit=64),
        run["id"],
    )
    return str(run["id"])


async def _start_run(
    settings: Any,
    trigger: Trigger,
    target: ManifestTarget,
    delivery: Delivery,
    payload: str,
    quarantined: bool,
) -> str:
    """The run, started as `trigger:<id>` with the trigger's scopes, on a fresh thread; its id."""
    from felix.durability.runs import start_durable_chat
    from felix.manifests.pin import pin_fields_for
    from felix.observability.metrics import record_counter
    from felix_ai.types import ChatMessage

    auth = _trigger_auth(trigger)
    thread = f"{trigger.tenant}:trigger:{trigger.id}:{uuid.uuid4().hex[:16]}"
    ctx = RequestContext(
        settings=settings, auth=auth, manifest_id=target.manifest, thread_id=thread, unattended=True
    )
    # Everything from here as the trigger's tenant: resolution, admission and the pin read the
    # manifest store, and the enqueue records this principal as the run's authority.
    async with async_run_with_context(ctx):
        resolved = await _admitted(settings, trigger, target, thread, auth)
        if quarantined:
            record_counter(
                "felix_content_screening",
                {"manifest_id": target.manifest, "tool": TRIGGER_EVENT_TOOL, "action": "quarantine"},
            )
        try:
            started = await start_durable_chat(
                settings,
                trigger.tenant,
                manifest_id=target.manifest,
                messages=[ChatMessage(role="user", content=render_prompt(trigger, delivery))],
                thread_id=thread,
                model_id=None,
                execution=resolved.manifest.spec.execution,
                pin=await pin_fields_for(
                    settings, trigger.tenant, resolved.manifest, version=resolved.version
                ),
                trigger_event=event_record(trigger, delivery, payload),
            )
        except Exception as exc:
            raise _Unavailable("the run could not be enqueued") from exc
    run_id = str(started["resume_token"])
    logger.info(
        "trigger_started trigger=%s event=%s run=%s", trigger.id, loggable(delivery.event, limit=64), run_id
    )
    return run_id


__all__ = ["router"]
