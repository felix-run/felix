"""Run due scheduled jobs — invoke the job's manifest, or start a run of its workflow."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import TYPE_CHECKING, Any, TypedDict

from felix.auth.actors import CRON
from felix.config import Settings
from felix.context import AuthContext, RequestContext, async_run_with_context
from felix.jobs import heartbeat as hb
from felix.jobs import store as jobs_store
from felix.jobs.schedule import ScheduleError, parse_schedule
from felix.logging_setup import loggable
from felix.patterns.types import ChatMessage, InvokeInput

if TYPE_CHECKING:
    from felix.manifests.schema import Manifest

logger = logging.getLogger("felix.jobs.scheduler")

# The machine actor a firing runs as, on behalf of the job's writer (`job_auth`), which also
# marks it `scheduled` -- what `jobs/agent_tools.py` reads to refuse scheduled work scheduling more.
CRON_ACTOR = CRON
# On a task's firing, `RequestContext.extras[SCHEDULED_FROM_EXTRA]` is `{"thread_id", "created_by"}`:
# the conversation `schedule_task` recorded (`payload.scheduled_by.thread_id`) and the job's writer.
# `jobs/origin_thread.py:origin_of` reads it -- for a `notify` the firing sends and for a task's report
# of its answer -- and names that thread only when the writer is who the run acts for.
SCHEDULED_FROM_EXTRA = "scheduled_from"


class ScheduledFrom(TypedDict):
    """`extras[SCHEDULED_FROM_EXTRA]`: where a task was scheduled from, and who wrote the job."""

    thread_id: str
    created_by: str


def now_ms() -> int:
    return int(time.time() * 1000)


def next_run_at_ms(schedule: str, from_ms: int | None = None) -> int | None:
    """When `schedule` next fires after `from_ms` (default: now). The grammar is `felix.jobs.schedule`.

    None for an empty schedule, which is manual only. Raises `ScheduleError` for a schedule
    outside the grammar -- never a guessed interval.
    """
    base = from_ms if from_ms is not None else now_ms()
    return parse_schedule(schedule).next_after(base)


def creator_auth(auth: AuthContext) -> dict[str, Any]:
    """What a job records of the caller writing it: the authority its runs may carry, and when.

    Exactly the writer's own (`felix.auth.recorded.record_caller`), so a job never reaches what
    its writer could not have reached by asking directly. `recorded_at` is the moment it was
    recorded -- the write -- for an operator reading `GET /jobs` to see how old a grant is.
    """
    from felix.auth.recorded import record_caller

    return {**record_caller(auth), "recorded_at": now_ms()}


_warned_unrecorded: set[tuple[str, str]] = set()


def job_auth(settings: Settings, tenant_id: str, job: dict[str, Any]) -> AuthContext:
    """The caller a firing runs as: `cron`, on behalf of whoever last wrote the job.

    `principal_sub` stays the machine, as a resumed fiber's does, so an audit row never says a
    person did what the worker did at 3am; `acting_for` is the writer, which is what
    `bind_principal`, approvals and `publish_commits` read. The scopes are the writer's as
    recorded, never widened -- and for a writer who authenticated with an API key, narrowed to
    what that key holds *now* (`_current_api_key_scopes`): a job outlives the request that wrote
    it, and a key an operator has removed or cut down must stop granting through it.

    A writer who signed in through Felix (GitHub, Google, GitHub Actions) is narrowed the same way,
    to what that sign-in grants in today's configuration (`_current_signin_scopes`): removing a
    person's Google entry or an org's grant stops their scheduled work, not only their next
    sign-in. A writer whose token came from another issuer keeps the grant recorded at the write:
    Felix keeps no record of that token beyond its claims, so there is nothing to re-check it
    against.

    A job written before writers were recorded has no `created_by`, and runs as what it always
    ran as -- a principal holding no scopes -- except that its manifest's inbound rules now apply,
    so one needing scopes or a particular scheme refuses it. Logged once per job and process.
    """
    from felix.auth.recorded import replay_caller

    creator = str(job.get("created_by") or "")
    recorded = job.get("created_auth") if isinstance(job.get("created_auth"), dict) else None
    if not creator or recorded is None:
        key = (tenant_id, str(job.get("name") or ""))
        if key not in _warned_unrecorded:
            _warned_unrecorded.add(key)
            logger.warning(
                "job_has_no_recorded_writer name=%s: running with no scopes; re-save it to run as its writer",
                loggable(key[1], limit=200),
            )
        return replay_caller(None, tenant_id=tenant_id, actor=CRON_ACTOR, scheduled=True)
    scheme = str(recorded.get("scheme") or "")
    if scheme in {"api_key", "apikey"}:
        recorded = _current_api_key_scopes(settings, tenant_id, creator, recorded)
    elif scheme == "self":
        recorded = _current_signin_scopes(settings, tenant_id, creator, recorded)
    return replay_caller(
        recorded, tenant_id=tenant_id, actor=CRON_ACTOR, on_behalf_of=creator, scheduled=True
    )


def rechecks_at_firing(settings: Settings, tenant_id: str, subject: str, recorded: dict[str, Any]) -> bool:
    """Whether each firing can re-check this writer's credential against today's configuration
    (`job_auth`): an API key, or a Felix sign-in whose grant for the tenant is configured here. A
    token from another issuer, or a sign-in to a personal tenant, has nothing to be re-checked
    against, and fires on the grant recorded at the write."""
    scheme = str(recorded.get("scheme") or "")
    if scheme in {"api_key", "apikey"}:
        return True
    if scheme == "self":
        idp = str(recorded.get("idp") or subject.split(":", 1)[0])
        return _signin_grant(settings, tenant_id, subject, idp, str(recorded.get("email") or "")) is not None
    return False


def _current_api_key_scopes(
    settings: Settings, tenant_id: str, subject: str, recorded: dict[str, Any]
) -> dict[str, Any]:
    """`recorded`, with its scopes cut to what the writer's API key holds in today's configuration.

    The key is found by the subject it authenticates as (`sub`, or `api_key` for a key with none)
    in this tenant. Several entries may share a subject; the job keeps what any of them grants, as
    the writer could today by presenting that one. None left -- the key removed, or the deployment
    no longer in `api_key` mode -- refuses the run: an `InboundAuthError`, so the run record says
    why rather than running with nothing.
    """
    from felix.auth.api_keys import parse_api_keys
    from felix.manifests.inbound_auth import InboundAuthError

    held: set[str] = set()
    found = False
    if getattr(settings, "auth_mode", "") == "api_key":
        try:
            # The door's own parser, so a key's tenant and subject default exactly as they do
            # there; malformed config refuses to boot, so `{}` here only means "nothing to find".
            entries = parse_api_keys(settings.auth_api_keys)
        except ValueError:
            entries = {}
        for entry in entries.values():
            if entry.tenant_id == tenant_id and entry.subject == subject:
                found = True
                held.update(entry.scopes)
    if not found:
        raise InboundAuthError(
            "the API key this job's writer used is no longer configured; re-save the job as someone "
            "who may run it",
            status_code=403,
        )
    scopes = recorded.get("scopes")
    kept = sorted(str(x) for x in scopes if str(x) in held) if isinstance(scopes, list) else []
    return {**recorded, "scopes": kept}


def _current_signin_scopes(
    settings: Settings, tenant_id: str, subject: str, recorded: dict[str, Any]
) -> dict[str, Any]:
    """`recorded`, with its scopes cut to what the writer's Felix sign-in grants in this tenant today.

    By the sign-in that minted the writer's token (`idp`, recorded since; else the subject's
    prefix):

    - **Google:** the account's `FELIX_GOOGLE_ACCOUNTS` entry, found by the recorded email (or, for
      a job recorded before emails were, by a `sub` pin), must still exist, name this tenant, and
      match the account's `sub` where it pins one.
    - **GitHub:** an org grant in `FELIX_GITHUB_ORG_TENANTS` must still map an org to this tenant.
      Whether the writer is still a member of it is GitHub's to say and is not asked at 3am; the
      grant being gone is what this catches. A personal signup tenant (`gh-<id>`) is the writer's
      own, and keeps what was recorded.
    - **GitHub Actions:** an org grant for this tenant must still let the workflow's repository in.

    None left refuses the run with an `InboundAuthError`, as a removed API key does.
    """
    from felix.manifests.inbound_auth import InboundAuthError

    idp = str(recorded.get("idp") or subject.split(":", 1)[0])
    held = _signin_grant(settings, tenant_id, subject, idp, str(recorded.get("email") or ""))
    if held is None:
        return recorded
    if not held:
        raise InboundAuthError(
            "the sign-in this job's writer used no longer grants this tenant; re-save the job as "
            "someone who may run it",
            status_code=403,
        )
    scopes = recorded.get("scopes")
    kept = sorted(str(x) for x in scopes if str(x) in held[1]) if isinstance(scopes, list) else []
    return {**recorded, "scopes": kept}


def _signin_grant(
    settings: Settings, tenant_id: str, subject: str, idp: str, email: str
) -> tuple[bool, frozenset[str]] | tuple[()] | None:
    """`(True, scopes)` when the writer's sign-in still grants this tenant; `()` when it no longer
    does; None when there is nothing to check it against (a personal tenant, an idp this does not
    know) and the record stands."""
    from felix.auth.github import parse_org_tenants
    from felix.auth.github_actions import ACTIONS_SUBJECT_PREFIX
    from felix.auth.github_signup import personal_tenant_owner
    from felix.auth.google import parse_accounts

    if idp == "google":
        try:
            accounts = parse_accounts(getattr(settings, "google_accounts", "") or "")
        except ValueError:
            accounts = {}
        google_sub = subject.removeprefix("google:")
        entry = accounts.get(email.strip().lower()) if email else None
        if entry is None:
            entry = next((a for a in accounts.values() if a.sub and a.sub == google_sub), None)
        if entry is None or entry.tenant != tenant_id or (entry.sub and entry.sub != google_sub):
            return ()
        return True, frozenset(entry.scopes)
    if idp in {"github", "github-actions"}:
        if idp == "github" and personal_tenant_owner(tenant_id) is not None:
            return None
        try:
            grants = parse_org_tenants(getattr(settings, "github_org_tenants", "") or "").values()
        except ValueError:
            grants = []
        mine = [g for g in grants if g.tenant == tenant_id]
        if idp == "github":
            held = frozenset(s for g in mine for s in g.scopes)
            return (True, held) if mine else ()
        repo = subject.removeprefix(ACTIONS_SUBJECT_PREFIX).split(":", 1)[0]
        allowed = [
            g.actions
            for g in mine
            if g.actions is not None and repo in {str(i) for i in g.actions.repositories.values()}
        ]
        return (True, frozenset(s for a in allowed for s in a.scopes)) if allowed else ()
    return None


async def _invoke_job_manifest(
    settings: Settings,
    *,
    tenant_id: str,
    job: dict[str, Any],
) -> dict[str, Any]:
    manifest_id = str(job.get("manifest_id") or "")
    if not manifest_id:
        return {"status": "skipped", "reason": "no_manifest"}

    from felix.tools.builtins import default_tool_provider

    payload = job.get("payload") or {}
    prompt = str(payload.get("prompt") or payload.get("message") or "ping")
    provider = default_tool_provider()

    auth = job_auth(settings, tenant_id, job)
    # One thread per job by default, so a digest job keeps its own history. `fresh_thread`
    # gives each firing a thread of its own: a job that works a *different* ticket every run
    # must not carry ticket N's transcript into ticket N+1's context.
    thread = f"{tenant_id}:job:{job['name']}"
    if payload.get("fresh_thread"):
        # A uuid, not the clock: two firings in one millisecond — a sweep that claims twice,
        # a test — would share a thread again, which is the one thing this exists to prevent.
        thread = f"{thread}:{uuid.uuid4().hex[:16]}"
    req_ctx = RequestContext(settings=settings, auth=auth, manifest_id=manifest_id, thread_id=thread)
    by = jobs_store.scheduled_by(job)
    if by is not None and isinstance(by.get("thread_id"), str) and by["thread_id"]:
        req_ctx.extras[SCHEDULED_FROM_EXTRA] = ScheduledFrom(
            thread_id=by["thread_id"], created_by=str(job.get("created_by") or "")
        )
    async with async_run_with_context(req_ctx):
        try:
            out = await _run_job_turn(settings, tenant_id, job, prompt=prompt, provider=provider, ctx=req_ctx)
        except Exception as exc:
            # Recorded here as `fire_job` would record it, so the run's sources are on it too: a
            # failed heartbeat's, and anything else that escapes the turn.
            out = _error_result(job, exc)
    return _with_unavailable_sources(out, req_ctx)


async def _run_job_turn(
    settings: Settings,
    tenant_id: str,
    job: dict[str, Any],
    *,
    prompt: str,
    provider: Any,
    ctx: RequestContext,
) -> dict[str, Any]:
    """The job's turn inside its request context -- resolve, check, compile, invoke -- and the
    result its run records. `_invoke_job_manifest` adds the run's unavailable sources to whatever
    this returns or raises, in one place, so no exit here can drop them."""
    from felix.runtime import build_tenant_agent, prepare_tenant_invoke, resolve_tenant_manifest

    req_ctx, auth, thread, manifest_id = ctx, ctx.auth, str(ctx.thread_id), ctx.manifest_id
    # Resolved inside the context, as a fiber's is: that is what binds the tenant for RLS.
    try:
        resolved = await resolve_tenant_manifest(settings, tenant_id, manifest_id, thread_id=thread)
    except LookupError:
        gone = await hb.manifest_missing(settings, tenant_id, job)
        if gone is None:
            raise
        return gone
    # A firing of a kind with its own prompt, manifest adjustments and record (a heartbeat's),
    # or None for a plain job, which runs its payload's prompt as it always has.
    firing = await hb.begin_firing(settings, tenant_id, job, resolved.manifest)
    if firing is not None:
        if firing.refusal is not None:
            return firing.refusal
        prompt = firing.prompt
    try:
        # The manifest's inbound auth -- `required_scopes`, `schemes`, `allow_anonymous`, a
        # credential's manifest pin -- and the thread's compile pin, as every other way into a
        # run checks them. A job used to compile straight past both, so `jobs:write` reached any
        # manifest in the tenant. A refusal raises, and is recorded as the run's error.
        await prepare_tenant_invoke(settings, resolved=resolved, auth=auth, thread_id=thread)
        if firing is not None:
            # After the checks above, which judged the manifest as stored.
            resolved.manifest = firing.adjust(resolved.manifest)
        # The prompt is job payload, writable with `jobs:write`. The compiled agent screens
        # it like any user turn; a refusal raises and is recorded as an error run.
        messages = [ChatMessage(role="user", content=prompt)]
        try:
            agent = await build_tenant_agent(
                settings,
                manifest=resolved.manifest,
                sub_agents=resolved.sub_agents,
                tools=provider,
                tenant_id=tenant_id,
                # The tenant's library only: a writer's personal skills are not recorded with the
                # job, and a job running on them would follow instructions its readers cannot see.
                skill_owner=None,
                # The writer's memory, though: a job runs for its writer, whose person key the
                # recorded caller carried (`AuthContext.person`).
                person=auth.person,
            )
            result = await agent.invoke(InvokeInput(messages=messages, thread_id=thread))
        finally:
            if firing is not None:
                await firing.after()
    except Exception as exc:
        if firing is not None:
            raise  # a heartbeat's failure is recorded by `fire_job`, as before; it has no report
        return await _failed_task(settings, tenant_id, job, resolved.manifest, req_ctx, exc)
    answer = result.final.content if result.final else ""
    if firing is not None:
        return firing.result(str(answer or ""), req_ctx.extras, thread)
    from felix.jobs.agent_tools import report_back

    # A task's answer, into the conversation it was scheduled from.
    report = await report_back(settings, tenant_id, job, resolved.manifest, req_ctx, str(answer or ""))
    out: dict[str, Any] = {"status": "ok", "answer": answer}
    if report is not None:
        out["report"] = report
    return out


def _with_unavailable_sources(result: dict[str, Any], ctx: RequestContext) -> dict[str, Any]:
    """The run's result, naming the tool sources its compile could not reach, by the manifest that
    named each -- the job's own, or a child it compiled (`felix.run_sources`). A run that answered
    without one of its sources is still `ok` -- it said so in its answer -- but the run record says
    it too, so an operator reading a quiet run's history can tell an outage from a quiet day; and a
    run that failed says it, since the missing source may be why."""
    from felix.run_sources import unavailable_sources

    unavailable = unavailable_sources(ctx.extras)
    return {**result, "unavailable_sources": unavailable} if unavailable else result


def _error_result(job: dict[str, Any], exc: Exception) -> dict[str, Any]:
    """A run that raised, as its record keeps it: logged with the exception, recorded with
    `_job_error_message` -- written for the readers of `GET /jobs/{name}/runs`, never its text."""
    logger.error("job_invoke_failed name=%s", loggable(str(job.get("name")), limit=200), exc_info=exc)
    return {"status": "error", "error": _job_error_message(exc)}


async def _failed_task(
    settings: Settings,
    tenant_id: str,
    job: dict[str, Any],
    manifest: Manifest,
    ctx: RequestContext,
    exc: Exception,
) -> dict[str, Any]:
    """A firing that raised, recorded as `fire_job` records one -- and, for a task, reported to the
    conversation it was scheduled from, so the person hears it did not finish rather than nothing."""
    from felix.jobs.agent_tools import report_back

    out = _error_result(job, exc)
    report = await report_back(settings, tenant_id, job, manifest, ctx, out["error"], failed=True)
    if report is not None:
        out["report"] = report
    return out


async def _start_job_workflow(settings: Settings, *, tenant_id: str, job: dict[str, Any]) -> dict[str, Any]:
    """Start a run of the job's workflow with `payload.inputs`, as `cron` for the job's writer.

    Starting is all a firing does: the run goes on in its own orchestrator fiber, and the job's
    run record names it (`workflow_run_id`) for `GET /workflows/{id}/runs/{run_id}`. The writer
    must hold `workflows:write` -- what starting a run over HTTP needs -- in the scopes the job
    recorded, and every manifest a step names is checked against them at the start, as it would
    be for the writer asking.

    The run is held to `workflow_revision`, the one current when the writer wrote the job (or the
    one they named, `PUT /jobs/{name}`). Its
    steps run with the writer's scopes, so a definition someone else saved since -- a new step, a
    tool with arguments of their choosing -- must not run as the writer until the writer re-saves
    the job. Each refusal is a written message; nothing is started.
    """
    from felix.auth.mgmt import SCOPE_WORKFLOWS_WRITE, holds_mgmt_scopes
    from felix.workflows import engine
    from felix.workflows import store as workflow_store

    workflow_id = str(job["workflow_id"])
    shown = workflow_id[:64]
    auth = job_auth(settings, tenant_id, job)
    if not holds_mgmt_scopes(settings, auth.scopes, SCOPE_WORKFLOWS_WRITE):
        return {"status": "error", "error": "the job's writer may not start workflow runs (workflows:write)"}
    inputs = (job.get("payload") or {}).get("inputs", {})
    if not isinstance(inputs, dict):
        return {"status": "error", "error": "payload.inputs must be an object of the workflow's inputs"}
    # The writer's context, not the asker's: `start_run` checks each step's manifest against the
    # caller in context and records them as the run's starter, whose scopes every step runs with.
    async with async_run_with_context(RequestContext(settings=settings, auth=auth)):
        workflow = await workflow_store.get_workflow(settings, tenant_id, workflow_id)
        if workflow is None:
            return {"status": "error", "error": f"workflow {shown} does not exist"}
        try:
            run = await engine.start_run(
                settings,
                tenant_id,
                workflow,
                inputs,
                trigger=f"job:{str(job['name'])[:200]}",
                started_by=auth.acting_for,
                revision=str(job.get("workflow_revision") or ""),
            )
        except engine.RevisionChanged:
            return {
                "status": "error",
                "error": f"workflow {shown} changed since this job was written; re-save the job to run it",
            }
        except engine.StartRefused as refused:
            return {"status": "error", "error": f"workflow {shown}: {refused.message}"}
    return {"status": "ok", "workflow_id": workflow_id, "workflow_run_id": run["id"]}


def _job_error_message(exc: BaseException) -> str:
    """A failed run's error, as its readers see it -- never the exception's own text unless its
    type is one written for a caller (`felix.errors`): an inbound refusal names the scopes it
    lacked, a screening refusal its code. Anything else is a fixed message; the exception is in
    the log."""
    from felix.errors import client_safe_message

    return client_safe_message(exc)


CREDENTIAL_EXPIRED = "the credential that scheduled this task has expired; schedule it again"


def _credential_expired(job: dict[str, Any], at: int) -> bool:
    """Whether the token that wrote this job had expired by `at`. Only a task (`jobs/agent_tools.py`)
    and a heartbeat whose credential no firing can re-check (`jobs/heartbeat.py:enrol`) record one
    (`created_auth.expires_at`); a job written any other way never has."""
    recorded = job.get("created_auth")
    expires_at = recorded.get("expires_at") if isinstance(recorded, dict) else None
    return isinstance(expires_at, int) and at >= expires_at


async def _record_expired(
    settings: Settings, tenant_id: str, job: dict[str, Any], started_at: int, trigger: str
) -> dict[str, Any]:
    """The run record of a firing refused because its writer's credential has expired."""
    message = hb.expired_message(job) or CREDENTIAL_EXPIRED
    run = await jobs_store.record_run(
        settings,
        tenant_id,
        job["name"],
        status="error",
        started_at=started_at,
        finished_at=now_ms(),
        error=message,
        result={"status": "error", "error": message, "trigger": trigger},
    )
    # Over this firing's own claim only, as every run's end is (`jobs_store.finish_run`).
    await jobs_store.finish_run(
        settings,
        tenant_id,
        job["name"],
        started_at=started_at,
        last_status="error",
        last_error=message,
    )
    return run


async def _clear_running(
    settings: Settings, tenant_id: str, job: dict[str, Any], started_at: int, error: str
) -> None:
    """End a run's `running` mark when the run ended some way other than the normal record.

    The claim marked the job `running`, and it is not claimable again while that mark stands
    (`jobs_store.claim_run`). Left behind, it would hold the job for `RUNNING_STALE_MS` for nothing.
    Best effort: the staleness bound is what covers a worker that cannot write at all.
    """
    try:
        await jobs_store.finish_run(
            settings, tenant_id, job["name"], started_at=started_at, last_status="error", last_error=error
        )
    except Exception:
        logger.warning("job_status_reset_failed name=%s", job.get("name"), exc_info=True)


async def fire_job(
    settings: Settings,
    tenant_id: str,
    job: dict[str, Any],
    *,
    started_at: int,
    trigger: str = "schedule",
    requested_by: str = "",
) -> dict[str, Any]:
    """Run one job now and record it. Returns the run row.

    The one path a job runs by, whether cron found it due or someone asked for it: the same
    identity (`cron` on behalf of the job's writer, with the writer's recorded scopes -- a manual
    run does not borrow the caller's), the same inbound checks, the same thread, the same
    screening of its prompt, the same run record. A manual run records its `trigger` and who
    asked, and leaves the schedule where it was.

    However it ends -- cancelled, or failed outside the work -- it leaves a run record, written
    before the `running` mark is cleared (`_end_abnormally`): a heartbeat's daily cap counts run
    records, so a firing that left none would be a model call nothing counted.
    """
    recorded: dict[str, Any] | None = None
    try:
        result: dict[str, Any] = {"status": "ok"}
        if _credential_expired(job, started_at):
            # Disabled first, so a sweep overlapping this one does not fire it again meanwhile.
            await jobs_store.disable_job(settings, tenant_id, job["name"])
            return await _record_expired(settings, tenant_id, job, started_at, trigger)
        work = (
            _start_job_workflow
            if job.get("workflow_id")
            else _invoke_job_manifest
            if job.get("manifest_id")
            else None
        )
        if work is not None:
            try:
                result = await work(settings, tenant_id=tenant_id, job=job)
            except Exception as exc:
                result = _error_result(job, exc)
        result["trigger"] = trigger
        if requested_by:
            result["requested_by"] = requested_by

        # `skipped`: a limit held the firing and nothing ran (`hb.over_daily_cap`). Anything else
        # that is not `ok` is an error.
        status = result["status"] if result.get("status") in {"ok", jobs_store.SKIPPED} else "error"
        run = await jobs_store.record_run(
            settings,
            tenant_id,
            job["name"],
            status=status,
            started_at=started_at,
            finished_at=now_ms(),
            error=str(result.get("error") or ""),
            result=result,
        )
        recorded = run
        # Do not write `enabled=True` back from a stale read — that silently
        # re-enabled a job an operator had just disabled. And never the schedule: a manual run
        # leaves it where it was, and a scheduled one was advanced by its claim (`claim_run`).
        # Writing it again here moved it *back* when a run outlasted its interval -- the next
        # tick had already claimed the next slot, and this rewrote that slot as due.
        #
        # And only over this run's own claim (`finish_run`): a run reclaimed as stale while it
        # still went on must not erase the mark of the run that reclaimed it.
        await jobs_store.finish_run(
            settings,
            tenant_id,
            job["name"],
            started_at=started_at,
            last_status=status,
            last_error=str(result.get("error") or ""),
        )
        return run
    except asyncio.CancelledError:
        # A worker shutting down, or a manual run's caller gone: the run ends here, so its mark does.
        # Shielded, so a second cancel cannot stop the record half-written; the model may already
        # have been called, and a heartbeat's cap must count it.
        try:
            await asyncio.shield(
                _end_abnormally(
                    settings, tenant_id, job, started_at, CANCELLED, trigger, requested_by, recorded
                )
            )
        except Exception:
            logger.warning("job_cancel_record_failed name=%s", job.get("name"), exc_info=True)
        raise
    except Exception:
        logger.exception("job_failed name=%s", job.get("name"))
        return await _end_abnormally(
            settings, tenant_id, job, started_at, EXECUTION_FAILED, trigger, requested_by, recorded
        )


# The error of a run that ended outside its work: cancelled (a worker shutting down, a manual run's
# caller gone), or failed in the scheduler itself.
CANCELLED = "cancelled"
EXECUTION_FAILED = "execution failed"


async def _end_abnormally(
    settings: Settings,
    tenant_id: str,
    job: dict[str, Any],
    started_at: int,
    error: str,
    trigger: str,
    requested_by: str,
    recorded: dict[str, Any] | None,
) -> dict[str, Any]:
    """Record the run as an error -- unless it was recorded already -- and only then clear its
    `running` mark. In that order: cleared first, a manual run could claim the job in between and
    find one run fewer to count than happened."""
    try:
        if recorded is None:
            result: dict[str, Any] = {"status": "error", "error": error, "trigger": trigger}
            if requested_by:
                result["requested_by"] = requested_by
            recorded = await jobs_store.record_run(
                settings,
                tenant_id,
                job["name"],
                status="error",
                started_at=started_at,
                finished_at=now_ms(),
                error=error,
                result=result,
            )
    finally:
        await _clear_running(settings, tenant_id, job, started_at, error)
    return recorded


async def run_due_jobs(settings: Settings, *, tenant_id: str = "default") -> int:
    """Execute enabled jobs whose next_run_at is due. Returns count fired."""
    jobs = await jobs_store.list_jobs(settings, tenant_id)
    fired = 0
    ts = now_ms()
    for job in jobs:
        if not job.get("enabled"):
            continue
        next_run = job.get("next_run_at")
        if next_run is not None and next_run > ts:
            continue
        try:
            parsed = parse_schedule(str(job.get("schedule") or ""))
        except ScheduleError as exc:
            # Stored before the schedule was validated on write. It used to fire every minute
            # whatever it said; now it does not fire, and says why where the operator looks.
            if job.get("last_error") != str(exc):
                logger.warning("job_schedule_invalid name=%s", loggable(str(job.get("name")), limit=200))
                await jobs_store.touch_run(
                    settings,
                    tenant_id,
                    job["name"],
                    last_run_at=job.get("last_run_at"),
                    next_run_at=jobs_store.KEEP_SCHEDULE,
                    last_status="error",
                    last_error=str(exc),
                )
            continue
        if parsed.one_shot:
            # Due at the time its write set, and claimed with none, so it fires once. With no due
            # time it has fired already (or was stored without one), and is never read as "due
            # now" the way a new recurring job is.
            if next_run is None:
                continue
            following = None
        else:
            following = parsed.next_after(ts)
            if following is None:
                # Manual only: it runs through `POST /jobs/{name}/run` and never from here.
                continue
        # Claim the job *before* invoking it. touch_run used to run only after the
        # invocation finished, so the every-minute cron re-fired the same job on every
        # tick until the first run completed. And the claim is conditional on the due time
        # this tick read, so a second worker -- or this worker's next tick, overlapping a
        # slow one -- that read the same due job does not fire it again.
        try:
            claimed = await jobs_store.claim_run(
                settings,
                tenant_id,
                job["name"],
                seen_next_run_at=next_run,
                last_run_at=ts,
                next_run_at=following,
            )
        except Exception:
            logger.warning("job_claim_failed name=%s", job.get("name"), exc_info=True)
            continue
        if not claimed:
            logger.info("job_already_claimed name=%s", job.get("name"))
            continue

        await fire_job(settings, tenant_id, job, started_at=ts)
        fired += 1
    return fired


async def run_due_jobs_all_tenants(settings: Settings) -> int:
    """Run due jobs for every tenant that has any. Returns total fired.

    ``run_due_jobs`` defaults to ``tenant_id="default"`` and the worker cron never passed
    one, so no other tenant's scheduled jobs ever fired.
    """
    from felix.db.session import rls_tenant

    total = 0
    for tenant_id in await jobs_store.list_tenants_with_jobs(settings):
        try:
            # Bind the tenant for the sweep. The worker has no request context, so nothing
            # else supplies `app.tenant_id`: under `FELIX_DATABASE_RLS` the policy then has no
            # tenant to match, filters every row, and this sweep reads an empty table and
            # reports success. Silent, and only on deployments where RLS is the isolation
            # mechanism -- the bundled compose role is superuser and skips the policy entirely.
            with rls_tenant(tenant_id):
                total += await run_due_jobs(settings, tenant_id=tenant_id)
        except Exception:
            # One tenant's bad job must not stop every other tenant's schedule.
            logger.exception("job_sweep_failed tenant=%s", loggable(tenant_id, limit=64))
    return total


__all__ = [
    "CRON_ACTOR",
    "SCHEDULED_FROM_EXTRA",
    "ScheduledFrom",
    "creator_auth",
    "fire_job",
    "job_auth",
    "next_run_at_ms",
    "rechecks_at_firing",
    "run_due_jobs",
    "run_due_jobs_all_tenants",
]
