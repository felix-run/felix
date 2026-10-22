"""`schedule_task`, `list_scheduled` and `cancel_scheduled`: an agent scheduling its own follow-up work.

Bound by `manifests/builder.py` for a manifest with `spec.scheduling`, before the governance
stack, so every wrapper applies -- and by default an approval rule (`scheduling_approval_rule`)
holds each `schedule_task` call until a person has read the prompt it would leave behind.

A task is an ordinary scheduled job (`jobs/store.py`) on the compiling manifest, written as the
caller: `created_by` is who the turn acts for and `created_auth` their authority, so a firing runs
as `cron` on their behalf (`jobs/scheduler.py:_job_auth`) and reaches nothing they could not reach
by asking. What marks it as a task is its payload's `scheduled_by`, and that is what `list_scheduled`
and `cancel_scheduled` filter on -- with the writer and the manifest -- so a caller sees and cancels
only the tasks they scheduled from this agent, never an operator's job or another person's task.

Every refusal is a result the model reads, `{"error": <code>, "detail": <message>}`, written here
and never an exception's text.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from felix.auth.mgmt import SCOPE_JOBS_READ, SCOPE_JOBS_WRITE
from felix.auth.recorded import token_expiry_ms
from felix.governance.permission_mode import SCHEDULE_TASK_TOOL_NAME, SCHEDULING_APPROVAL_RULE_ID
from felix.tools.refusal import ToolRefused, require_person
from felix.tools.types import Tool, ToolInput, ToolInvocationCtx, define_tool

if TYPE_CHECKING:
    from felix.context import AuthContext
    from felix.jobs.schedule import Schedule
    from felix.manifests.schema import ApprovalRule, SchedulingSpec

logger = logging.getLogger("felix.jobs.agent_tools")

SCHEDULE_TASK = SCHEDULE_TASK_TOOL_NAME
SCHEDULING_TOOL_NAMES = frozenset({SCHEDULE_TASK, "list_scheduled", "cancel_scheduled"})

MAX_PROMPT_CHARS = 8000
MAX_TITLE_CHARS = 200
# How far ahead a one-shot may be set. A task a year out outlives most reasons to have set it.
MAX_AHEAD_MS = 366 * 86_400_000
# How many of a cron's next firings `_shortest_gap_ms` walks. Enough to cross a week of a
# weekday-only schedule many times over; cheap, since each step jumps whole days or hours.
_GAP_SAMPLES = 200

_WHEN_DESCRIPTION = (
    "When the task runs, in UTC. One of: at:<ISO-8601 timestamp with offset> to run once "
    "(at:2026-10-21T09:00:00Z, at:2026-10-21T11:00:00+02:00 -- an offset is required); "
    "every:<n><m|h> to repeat at an interval (every:90m, every:6h); @hourly, @daily, @weekly, "
    "@monthly; or a five-field cron expression evaluated in UTC (0 9 * * 1-5 is 09:00 UTC on "
    "weekdays)."
)


class _ScheduleTaskArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    when: str = Field(description=_WHEN_DESCRIPTION, json_schema_extra={"minLength": 1, "maxLength": 200})
    # Lengths are refused by the handler, with a message for the model, and advertised here.
    prompt: str = Field(
        description=(
            "The instruction the task runs, exactly as a new user message to this agent. It runs "
            "later with no conversation around it, so make it self-contained: say what to do and "
            "everything needed to do it."
        ),
        json_schema_extra={"minLength": 1, "maxLength": MAX_PROMPT_CHARS},
    )
    title: str = Field(
        default="",
        description="Optional: a short label for the task, shown when tasks are listed.",
        json_schema_extra={"maxLength": MAX_TITLE_CHARS},
    )
    fresh_thread: bool = Field(
        default=False,
        description=(
            "Run each firing in a new conversation. By default every firing of the task shares "
            "one conversation, so it sees what earlier firings said."
        ),
    )


class _ListScheduledArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _CancelScheduledArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        min_length=1,
        max_length=64,
        description="The task's name, as schedule_task or list_scheduled gave it.",
    )


def _iso(ms: Any) -> str | None:
    if not isinstance(ms, int):
        return None
    return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat().replace("+00:00", "Z")


def _now_ms() -> int:
    from felix.jobs.store import now_ms

    return now_ms()


def scheduling_approval_rule() -> ApprovalRule:
    """The rule a manifest gets for `schedule_task` when `spec.scheduling.approval` is on: single-use
    and for the person who asked, so an approval is never a standing licence to schedule."""
    from felix.manifests.schema import ApprovalRule

    return ApprovalRule(
        id=SCHEDULING_APPROVAL_RULE_ID,
        description="Approve scheduling this task",
        tools=[SCHEDULE_TASK],
        one_shot=True,
        bind_principal=True,
    )


def default_scheduling_rule(
    scheduling: SchedulingSpec | None, approvals: list[ApprovalRule]
) -> ApprovalRule | None:
    """The approval rule to add for `schedule_task`, or None.

    None when scheduling is off, when `approval` is off, or when one of the manifest's own rules
    names `schedule_task` *literally* and gates every call to it. A glob does not count: as a
    literal ours beats it in `apply_approvals`, so `approvals: [{tools: ["*"]}]` leaves the call
    under the built-in one-shot, principal-bound rule rather than a looser catch-all. Nor does a
    rule with `when_args`, which gates only some calls; ours, the later literal, is selected.
    """
    if scheduling is None or not scheduling.approval:
        return None
    if any(SCHEDULE_TASK in r.tools and not r.when_args for r in approvals):
        return None
    return scheduling_approval_rule()


def _shortest_gap_ms(parsed: Schedule, now: int) -> int | None:
    """The shortest gap between firings of a recurring schedule, or None for one that does not
    recur (manual only, or a one-shot). A cron's gaps are uneven (`0 9,10 * * *`), so its next
    firings from now are walked and the smallest gap taken."""
    if parsed.one_shot:
        return None
    if parsed.interval_ms is not None:
        return parsed.interval_ms
    if parsed.cron is None:
        return None
    gaps: list[int] = []
    at = parsed.next_after(now)
    for _ in range(_GAP_SAMPLES):
        if at is None:
            break
        following = parsed.next_after(at)
        if following is None:
            break
        gaps.append(following - at)
        at = following
    return min(gaps) if gaps else None


class _AgentTasks:
    """One manifest's task calls, bound to its tenant and `spec.scheduling`. Every refusal raises
    `ToolRefused`; the tool wrappers in `make_scheduling_tools` turn it into the result."""

    def __init__(self, settings: Any, *, tenant_id: str, manifest_id: str, spec: SchedulingSpec) -> None:
        self.settings, self.tenant_id, self.manifest_id, self.spec = settings, tenant_id, manifest_id, spec

    def caller(self, scope: str, *, scheduling: bool) -> AuthContext:
        """The caller behind this call. ``scheduling`` asks the one question only `schedule_task`
        asks: whether this is a scheduled run, which may not schedule more."""
        from felix.auth.mgmt import holds_mgmt_scopes

        # A task is owned by its writer's subject: list and cancel match on it. A subject every
        # holder of a credential shares would make each one's tasks everyone's.
        _ctx, auth = require_person(
            no_caller=(
                "scheduled tasks belong to a signed-in person, and this conversation has none; "
                "nothing was scheduled or changed"
            ),
            shared_subject=(
                "the credential this conversation uses names no person (it is shared), so a task "
                "could not belong to anyone; nothing was scheduled or changed"
            ),
        )
        if scheduling and auth.scheduled:
            raise ToolRefused(
                "scheduled_run",
                "this is a scheduled task running; a scheduled run cannot schedule further work",
            )
        if not holds_mgmt_scopes(self.settings, auth.scopes, scope):
            raise ToolRefused(
                "missing_scope", f"managing scheduled tasks needs the {scope} scope, which this caller lacks"
            )
        return auth

    async def mine(self, writer: str) -> list[dict[str, Any]]:
        """The tasks ``writer`` scheduled from this manifest -- and nothing else in the tenant."""
        from felix.jobs import store as jobs_store

        found = []
        for job in await jobs_store.list_jobs(self.settings, self.tenant_id):
            by = (job.get("payload") or {}).get("scheduled_by")
            if (
                isinstance(by, dict)
                and by.get("tool") == SCHEDULE_TASK
                and by.get("manifest") == self.manifest_id
                and job.get("manifest_id") == self.manifest_id
                and job.get("created_by") == writer
            ):
                found.append(job)
        return found

    def check_when(self, when: str, now: int) -> Schedule:
        from felix.errors import client_safe_message
        from felix.jobs.schedule import ScheduleError, parse_schedule

        try:
            parsed = parse_schedule(when)
        except ScheduleError as exc:
            raise ToolRefused(
                "invalid_schedule", client_safe_message(exc, authored_for_clients=True)
            ) from None
        if parsed.at_ms is not None:
            if parsed.at_ms <= now:
                raise ToolRefused("in_the_past", "that time has already passed; nothing was scheduled")
            if parsed.at_ms - now > MAX_AHEAD_MS:
                raise ToolRefused(
                    "too_far_ahead", "a task can be set at most 366 days ahead; nothing was scheduled"
                )
            return parsed
        if parsed.next_after(now) is None:
            raise ToolRefused(
                "never_fires",
                "a task must have a time to run: give at:<timestamp>, an interval, or a cron expression",
            )
        gap = _shortest_gap_ms(parsed, now)
        floor = self.spec.min_interval_minutes
        if gap is not None and gap < floor * 60_000:
            raise ToolRefused(
                "too_frequent",
                f"this schedule fires every {max(gap // 60_000, 1)} minute(s) at its shortest; a "
                f"recurring task here may fire at most once every {floor} minutes",
            )
        return parsed

    async def admit(self, args: _ScheduleTaskArgs, now: int) -> tuple[AuthContext, Schedule]:
        """Every check `schedule_task` makes before it writes, in the order a refusal is reported:
        run by the call itself and, first, by its approval preview."""
        auth = self.caller(SCOPE_JOBS_WRITE, scheduling=True)
        _check_text(args)
        parsed = self.check_when(args.when, now)
        expires_at = token_expiry_ms(auth)
        if parsed.at_ms is not None and expires_at is not None and parsed.at_ms > expires_at:
            raise ToolRefused(
                "outlives_credential",
                f"that time is after the credential this conversation uses expires ({_iso(expires_at)}); "
                "a task cannot run on authority that will have lapsed. Choose an earlier time",
            )
        live = [j for j in await self.mine(auth.acting_for) if _is_live(j)]
        if len(live) >= self.spec.max_tasks:
            raise ToolRefused(
                "too_many_tasks",
                f"this person already has {len(live)} scheduled tasks here, the most allowed "
                f"({self.spec.max_tasks}); cancel one with cancel_scheduled first",
            )
        return auth, parsed

    async def schedule(self, args: _ScheduleTaskArgs, ctx: ToolInvocationCtx | None) -> str:
        from felix.jobs import store as jobs_store
        from felix.jobs.schedule import ScheduleError

        now = _now_ms()
        auth, parsed = await self.admit(args, now)
        try:
            row = await jobs_store.put_job(
                self.settings,
                self.tenant_id,
                f"task-{uuid.uuid4().hex[:12]}",
                schedule=args.when,
                manifest_id=self.manifest_id,
                payload=self.payload(args, ctx),
                created_by=auth.acting_for,
                created_auth=self.recorded(auth),
                # A recurring task waits for its schedule: "every day at 9" first runs at 9, not
                # on the next sweep as a job written over HTTP does. A one-shot ignores it.
                first_run_at=parsed.next_after(now),
            )
        except ScheduleError:
            # `check_when` read the same string a moment ago; only the clock moving past an
            # `at:` between the two lands here.
            raise ToolRefused("in_the_past", "that time has already passed; nothing was scheduled") from None
        except Exception:
            logger.warning("schedule_task failed manifest=%s", self.manifest_id, exc_info=True)
            raise ToolRefused(
                "schedule_failed", "the task could not be saved; nothing was scheduled"
            ) from None
        return json.dumps(
            {
                "status": "scheduled",
                "name": row["name"],
                "schedule": row["schedule"],
                "next_run_at": _iso(row.get("next_run_at")),
                "title": args.title or None,
            }
        )

    def recorded(self, auth: AuthContext) -> dict[str, Any]:
        """The writer's authority as every job records it, plus -- for a task only -- when the
        token behind it expires. A firing past that is refused and the task disabled
        (`scheduler.fire_job`): a task's life is capped at its writer's credential, as a durable
        run's is. None for a credential without one (an API key)."""
        from felix.jobs.scheduler import creator_auth

        return {**creator_auth(auth), "expires_at": token_expiry_ms(auth)}

    def payload(self, args: _ScheduleTaskArgs, ctx: ToolInvocationCtx | None) -> dict[str, Any]:
        """The job's payload: what a firing reads (`prompt`, `fresh_thread`) and what marks it as
        this manifest's task, with the conversation it was scheduled from."""
        from felix.context import try_get_context

        req = try_get_context()
        thread_id = (ctx.thread_id if ctx is not None else None) or (req.thread_id if req else None)
        return {
            "prompt": args.prompt,
            "title": args.title or None,
            "fresh_thread": args.fresh_thread,
            "scheduled_by": {"tool": SCHEDULE_TASK, "manifest": self.manifest_id, "thread_id": thread_id},
        }

    async def list_tasks(self) -> str:
        auth = self.caller(SCOPE_JOBS_READ, scheduling=False)
        tasks = [
            {
                "name": j["name"],
                "title": (j.get("payload") or {}).get("title"),
                "schedule": j["schedule"],
                "next_run_at": _iso(j.get("next_run_at")),
                "last_run_at": _iso(j.get("last_run_at")),
                "last_status": j.get("last_status") or None,
                "enabled": bool(j.get("enabled")),
                "expires_at": _iso((j.get("created_auth") or {}).get("expires_at")),
            }
            for j in await self.mine(auth.acting_for)
        ]
        return json.dumps({"tasks": tasks})

    async def cancel(self, args: _CancelScheduledArgs) -> str:
        from felix.jobs import store as jobs_store

        auth = self.caller(SCOPE_JOBS_WRITE, scheduling=False)
        # The same answer for a name that does not exist and one that is not the caller's: a
        # different one would tell the model which job names other people hold.
        if args.name not in {j["name"] for j in await self.mine(auth.acting_for)}:
            raise ToolRefused("no_such_task", "there is no task of that name among this person's tasks")
        await jobs_store.delete_job(self.settings, self.tenant_id, args.name)
        return json.dumps({"status": "cancelled", "name": args.name})

    async def preview(self, args: ToolInput) -> str:
        """What an approver reads: the schedule and the prompt that will run, as the call gives them.

        Runs `admit` first and raises its `ToolRefused`: the approvals wrapper then denies the call
        with no row and no wait (`builder._approval_preview`). A call the tool would refuse -- a
        scheduled run, an anonymous caller, an over-cap or too-frequent task -- must never put its
        prompt in front of an approver, nor park a cron firing on an approval nobody will give.
        """
        from pydantic import ValidationError

        try:
            parsed = _ScheduleTaskArgs.model_validate(args)
        except ValidationError:
            raise ToolRefused(
                "invalid_arguments", "the arguments do not match schedule_task's schema"
            ) from None
        await self.admit(parsed, _now_ms())
        lines = [f"schedule_task on {self.manifest_id}: {parsed.when}"]
        if parsed.title:
            lines.append(f"Title: {parsed.title}")
        if parsed.fresh_thread:
            lines.append("Each firing starts a new conversation.")
        lines.append("Runs later, with no one watching, as the person who asked.")
        return "\n".join(lines) + "\n\n--- prompt ---\n" + parsed.prompt


def _check_text(args: _ScheduleTaskArgs) -> None:
    if not args.prompt.strip() or len(args.prompt) > MAX_PROMPT_CHARS:
        raise ToolRefused("invalid_prompt", f"the prompt must be between 1 and {MAX_PROMPT_CHARS} characters")
    if len(args.title) > MAX_TITLE_CHARS:
        raise ToolRefused("invalid_title", f"the title must be at most {MAX_TITLE_CHARS} characters")


def is_fired_task(job: dict[str, Any]) -> bool:
    """A task `schedule_task` wrote that will not run again -- a one-shot that has fired, or any
    task disabled when its writer's credential expired -- and has run: what retention may delete.
    Never an operator's job, whatever its schedule or state."""
    by = (job.get("payload") or {}).get("scheduled_by")
    return (
        isinstance(by, dict)
        and by.get("tool") == SCHEDULE_TASK
        and job.get("last_run_at") is not None
        and not _is_live(job)
    )


def _is_live(job: dict[str, Any]) -> bool:
    """Whether a task still holds a place: a one-shot that has fired does not, though it is listed
    (and can be cancelled) until someone removes it."""
    from felix.jobs.schedule import ScheduleError, parse_schedule

    if not job.get("enabled"):
        # Disabled when its writer's credential expired (`scheduler.fire_job`): it will not run.
        return False
    try:
        one_shot = parse_schedule(str(job.get("schedule") or "")).one_shot
    except ScheduleError:
        return True
    return not (one_shot and job.get("next_run_at") is None)


def make_scheduling_tools(
    settings: Any, *, tenant_id: str, manifest_id: str, spec: SchedulingSpec
) -> list[Tool]:
    """`schedule_task`, `list_scheduled` and `cancel_scheduled` for one manifest in one tenant."""
    tasks = _AgentTasks(settings, tenant_id=tenant_id, manifest_id=manifest_id, spec=spec)

    async def _schedule(args: _ScheduleTaskArgs, ctx: ToolInvocationCtx | None = None) -> str:
        try:
            return await tasks.schedule(args, ctx)
        except ToolRefused as refused:
            return refused.result()

    async def _list(_args: _ListScheduledArgs) -> str:
        try:
            return await tasks.list_tasks()
        except ToolRefused as refused:
            return refused.result()

    async def _cancel(args: _CancelScheduledArgs) -> str:
        try:
            return await tasks.cancel(args)
        except ToolRefused as refused:
            return refused.result()

    floor = spec.min_interval_minutes
    schedule = define_tool(
        name=SCHEDULE_TASK,
        description=(
            "Schedule follow-up work: run a prompt through this agent later, once or on a "
            "repeating schedule, on behalf of the person you are talking to. Use it for a "
            "reminder, a check to repeat, or work that should happen at a set time. A repeating "
            f"task may fire at most once every {floor} minutes; a one-off may be set up to 366 "
            f"days ahead; one person may hold {spec.max_tasks} tasks here. Returns the task's name "
            "and when it next runs."
        ),
        args=_ScheduleTaskArgs,
        handler=_schedule,
    )
    schedule.approval_preview = tasks.preview
    listing = define_tool(
        name="list_scheduled",
        description=(
            "List the tasks the person you are talking to has scheduled with this agent: name, "
            "title, schedule, and when each last and next runs (UTC)."
        ),
        args=_ListScheduledArgs,
        handler=_list,
        read_only=True,
    )
    cancel = define_tool(
        name="cancel_scheduled",
        description="Cancel one of this person's scheduled tasks by name; it will not run again.",
        args=_CancelScheduledArgs,
        handler=_cancel,
    )
    return [schedule, listing, cancel]


__all__ = [
    "SCHEDULE_TASK",
    "SCHEDULING_APPROVAL_RULE_ID",
    "SCHEDULING_TOOL_NAMES",
    "default_scheduling_rule",
    "is_fired_task",
    "make_scheduling_tools",
    "scheduling_approval_rule",
]
