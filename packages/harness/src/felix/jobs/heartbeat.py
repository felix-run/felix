"""Heartbeats: a periodic check-in a person turns on for themselves, on a manifest that offers one.

The operator defines it (`spec.heartbeat`: what to check, how often, on which model, how far a
firing may go); a person turns it on or off (`routes/heartbeats.py`), and nothing else. Enrolling
writes an ordinary job (`jobs/store.py`) as that person -- `created_by` them, `created_auth` their
authority as every job records it -- so a firing runs as `cron` on their behalf through the
manifest's inbound auth (`scheduler.job_auth`), unattended, and reaches nothing they could not
reach by asking. A credential each firing can re-check -- an API key, a Felix sign-in that grants
this tenant -- records no expiry: `job_auth` narrows it to today's configuration every time. Any
other (a token from another issuer, a personal-tenant sign-in) has nothing to re-check against, so
its token's `exp` is recorded and the heartbeat stops there, as a task does
(`scheduler.CREDENTIAL_EXPIRED`).

What marks the job as a heartbeat is its payload's `scheduled_by` (`tool: heartbeat`), which only
`enrol` writes -- `PUT /jobs` drops a caller's and keeps the stored one only for its own writer --
with the person's heartbeat conversation (`{tenant}:hb-<hex>`), where `notify` leaves each notice.
A firing never reads instructions from the job: its prompt is built from the manifest's *current*
`checklist` (`firing_prompt`), so an operator's edit applies from the next firing and no one with
`jobs:write` can put words in it. Each firing runs in a fresh thread, and after it the oldest
firing threads past `keep_threads` are deleted (`prune_firing_threads`); the conversation is never
one of them. A firing past the manifest's `max_firings_per_day` in the current UTC day -- scheduled or
asked for -- invokes nothing and is recorded `skipped`, reason `daily_cap` (`over_daily_cap`).
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from felix.config import Settings
    from felix.context import AuthContext
    from felix.manifests.schema import HeartbeatSpec, Manifest

logger = logging.getLogger("felix.jobs.heartbeat")

# `payload.scheduled_by.tool` of a heartbeat's job, as `schedule_task` is a task's.
HEARTBEAT = "heartbeat"
JOB_PREFIX = "hb-"
# The person's heartbeat conversation is `{tenant}:hb-<16 hex>`: a chat thread id (no `:` in the
# suffix), so a chat client can open it and `notify` will write into it
# (`thread_ids.py:openable_thread`).
CONVERSATION_PREFIX = "hb-"
HEARTBEAT_OK = "HEARTBEAT_OK"
MAX_ANSWER_CHARS = 500
NO_HEARTBEAT = "this manifest no longer defines a heartbeat; it has been turned off"
NO_MANIFEST = "this heartbeat's manifest no longer exists; it has been turned off"
CREDENTIAL_EXPIRED = "the credential that turned this heartbeat on has expired; turn it on again"
# The `reason` a skipped run records (`over_daily_cap`) when the heartbeat had already run
# `max_firings_per_day` times in the current UTC day.
DAILY_CAP = "daily_cap"
_MINT_ATTEMPTS = 5
_FIRING_SUFFIX = re.compile(r"[0-9a-f]{16}")

FIRING_FRAME = (
    "This is a scheduled heartbeat check, run for the person you work for while nobody is "
    "watching. Use your tools to check the following.\n\n"
    "--- checklist ---\n{checklist}\n--- end of checklist ---\n\n"
    f"If nothing needs the person's attention, reply with exactly {HEARTBEAT_OK} and do not call "
    "notify. If something does, call notify once, with a short title and a message saying what "
    "needs them, then reply briefly."
)


class HeartbeatRefused(Exception):
    """Enrolment refused. ``message`` is written for the caller; ``status`` is the HTTP answer."""

    def __init__(self, code: str, message: str, *, status: int) -> None:
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.status = code, message, status


def job_name(manifest_id: str, person: str) -> str:
    """The job of `person`'s heartbeat on `manifest_id`: one per person per manifest, the same
    every time. The person is hashed, so a subject's characters never reach a job name (a URL path
    segment) and the name says nothing of who it is for. Raises `ValueError` for a manifest name a
    job name and thread id cannot carry."""
    from felix.manifests.schema import MANIFEST_NAME_RE

    if not manifest_id or len(manifest_id) > 128 or not MANIFEST_NAME_RE.match(manifest_id):
        raise ValueError("not a manifest name")
    digest = hashlib.sha256(person.encode()).hexdigest()[:12]
    return f"{JOB_PREFIX}{manifest_id}-{digest}"


def origin(job: dict[str, Any]) -> dict[str, Any] | None:
    """The job's `scheduled_by`, when it marks a heartbeat on the manifest the job runs."""
    from felix.jobs.store import scheduled_by

    return scheduled_by(job, HEARTBEAT)


def is_heartbeat_job(job: dict[str, Any]) -> bool:
    return origin(job) is not None


def firing_prompt(checklist: str) -> str:
    """The user turn of a firing: the operator's checklist in the fixed frame."""
    return FIRING_FRAME.format(checklist=checklist.strip())


def for_firing(manifest: Manifest, heartbeat: HeartbeatSpec) -> Manifest:
    """`manifest` as a firing runs it: on `heartbeat.model` when set, and bounded at
    `heartbeat.max_turns` steps -- `recursion_limit` for a react loop, `max_turns` for a
    multi-agent pattern -- or the manifest's own bound where that is lower: a heartbeat only ever
    narrows how far a run may go. Applied after the inbound and pin checks, which judge it as
    stored."""
    from felix.patterns.react import DEFAULT_RECURSION

    own_recursion = manifest.spec.recursion_limit or DEFAULT_RECURSION
    spec_update: dict[str, Any] = {
        "recursion_limit": min(heartbeat.max_turns, own_recursion),
        "max_turns": min(heartbeat.max_turns, manifest.spec.max_turns),
    }
    if heartbeat.model:
        spec_update["model"] = manifest.spec.model.model_copy(update={"id": heartbeat.model})
    spec = manifest.spec.model_copy(update=spec_update)
    return manifest.model_copy(update={"spec": spec})


def firing_result(answer: str, *, sent: int, thread_id: str) -> dict[str, Any]:
    """What a firing's run records: whether it was quiet -- sent no notice -- and its answer, cut."""
    return {"status": "ok", "quiet": sent == 0, "answer": answer[:MAX_ANSWER_CHARS], "thread_id": thread_id}


def firing_thread_prefix(tenant_id: str, name: str) -> str:
    """The prefix of every firing thread of job `name` (`scheduler._invoke_job_manifest` with
    `fresh_thread`): `{tenant}:job:<name>:`. The person's conversation never starts with it."""
    return f"{tenant_id}:job:{name}:"


async def prune_firing_threads(settings: Settings, tenant_id: str, job: dict[str, Any], keep: int) -> int:
    """Delete this heartbeat's firing threads past the newest `keep`, whole. Never its person's
    conversation. How many were deleted."""
    from felix.session.prune import delete_threads, threads_under

    by = origin(job) or {}
    conversation = by.get("thread_id")
    prefix = firing_thread_prefix(tenant_id, str(job["name"]))
    found = await threads_under(settings, tenant_id=tenant_id, prefix=prefix)
    found.pop(str(conversation or ""), None)
    # Only what a firing mints after the prefix (`uuid4().hex[:16]`): job names are not validated,
    # so another job named `<this name>:x` has threads under the same prefix, and they are not ours.
    found = {tid: ts for tid, ts in found.items() if _FIRING_SUFFIX.fullmatch(tid[len(prefix) :])}
    # Newest first; the id breaks a tie so both store arms agree on which is kept.
    ordered = sorted(found, key=lambda thread_id: (found[thread_id], thread_id), reverse=True)
    doomed = ordered[max(keep, 0) :]
    return await delete_threads(settings, tenant_id=tenant_id, thread_ids=doomed)


async def _conversation(settings: Settings, tenant_id: str, manifest_id: str, kept: str | None) -> str:
    """The person's heartbeat conversation: `kept` if it still exists, else a new one, created here
    (insert-if-absent, so two enrolments never share one) and titled for the session list."""
    from felix.session.store import get_session_store
    from felix.session.thread_state import LAST_MANIFEST_KEY, claim_thread, thread_exists

    store = get_session_store(settings, tenant_id=tenant_id)
    if kept and kept.startswith(f"{tenant_id}:{CONVERSATION_PREFIX}"):
        if await thread_exists(store.open(kept), settings=settings, tenant_id=tenant_id):
            return kept
    for _ in range(_MINT_ATTEMPTS):
        thread_id = f"{tenant_id}:{CONVERSATION_PREFIX}{uuid.uuid4().hex[:16]}"
        if await thread_exists(store.open(thread_id), settings=settings, tenant_id=tenant_id):
            continue
        if await claim_thread(
            settings=settings,
            tenant_id=tenant_id,
            thread_id=thread_id,
            session_name=f"Heartbeat: {manifest_id}",
            **{LAST_MANIFEST_KEY: manifest_id},
        ):
            return thread_id
    raise HeartbeatRefused(
        "conversation_unavailable", "the heartbeat conversation could not be created", status=503
    )


def public(job: dict[str, Any]) -> dict[str, Any]:
    """A heartbeat as its person sees it. `expires_at` is when it stops for good -- its enrolling
    credential's expiry, for one no firing can re-check -- or None."""
    from datetime import UTC, datetime

    by = origin(job) or {}
    recorded = job.get("created_auth")
    expires = recorded.get("expires_at") if isinstance(recorded, dict) else None
    return {
        "expires_at": (
            datetime.fromtimestamp(expires / 1000, tz=UTC).isoformat().replace("+00:00", "Z")
            if isinstance(expires, int)
            else None
        ),
        "manifest": job.get("manifest_id"),
        "name": job.get("name"),
        "enabled": bool(job.get("enabled")),
        "schedule": job.get("schedule"),
        "thread_id": by.get("thread_id"),
        "next_run_at": job.get("next_run_at"),
        "last_run_at": job.get("last_run_at"),
        "last_status": job.get("last_status") or None,
    }


async def mine(settings: Settings, tenant_id: str, person: str) -> list[dict[str, Any]]:
    """`person`'s heartbeats in the tenant, and nothing else."""
    from felix.jobs import store as jobs_store

    return [
        job
        for job in await jobs_store.list_jobs(settings, tenant_id)
        if is_heartbeat_job(job) and job.get("created_by") == person
    ]


async def get_mine(
    settings: Settings, tenant_id: str, manifest_id: str, person: str
) -> dict[str, Any] | None:
    """`person`'s heartbeat on `manifest_id`, or None."""
    from felix.jobs import store as jobs_store

    try:
        name = job_name(manifest_id, person)
    except ValueError:
        return None
    job = await jobs_store.get_job(settings, tenant_id, name)
    if job is None or not is_heartbeat_job(job) or job.get("created_by") != person:
        return None
    return job


async def enrol(
    settings: Settings, *, tenant_id: str, auth: AuthContext, manifest_id: str, enabled: bool
) -> dict[str, Any]:
    """Turn `auth`'s person's heartbeat on `manifest_id` on (or off, keeping it), and return the job.

    Refused (`HeartbeatRefused`) for a manifest that does not exist (404), for a person the
    manifest's inbound rules would refuse at the firing -- checked as the firing's own caller
    (`cron` for them, with their recorded scopes), raising `InboundAuthError` -- and only then for a
    manifest that defines no heartbeat (422). In that order, so a caller the manifest refuses learns
    nothing of whether it offers one.
    """
    from felix.auth.recorded import token_expiry_ms
    from felix.jobs import store as jobs_store
    from felix.jobs.schedule import parse_schedule
    from felix.jobs.scheduler import creator_auth, job_auth, now_ms, rechecks_at_firing
    from felix.runtime import prepare_tenant_invoke, resolve_tenant_manifest

    person = auth.acting_for
    try:
        name = job_name(manifest_id, person)
    except ValueError:
        raise HeartbeatRefused("unknown_manifest", "there is no manifest of that name", status=404) from None
    try:
        resolved = await resolve_tenant_manifest(settings, tenant_id, manifest_id)
    except LookupError:
        raise HeartbeatRefused("unknown_manifest", "there is no manifest of that name", status=404) from None

    recorded = creator_auth(auth)
    if not rechecks_at_firing(settings, tenant_id, person, recorded):
        # Nothing to re-check this credential against at 3am: its own expiry is the bound.
        recorded["expires_at"] = token_expiry_ms(auth)
    # The caller each firing will run as, asked now. Raises `InboundAuthError`, which the route
    # answers with its own written message.
    firing_auth = job_auth(
        settings, tenant_id, {"name": name, "created_by": person, "created_auth": recorded}
    )
    await prepare_tenant_invoke(settings, resolved=resolved, auth=firing_auth, thread_id=None)
    heartbeat = resolved.manifest.spec.heartbeat
    if heartbeat is None:
        raise HeartbeatRefused("no_heartbeat", "this manifest does not define a heartbeat", status=422)

    existing = await jobs_store.get_job(settings, tenant_id, name)
    kept = None
    if existing is not None and existing.get("created_by") == person:
        kept = (origin(existing) or {}).get("thread_id")
    thread_id = await _conversation(settings, tenant_id, manifest_id, kept if isinstance(kept, str) else None)
    now = now_ms()
    return await jobs_store.put_job(
        settings,
        tenant_id,
        name,
        schedule=heartbeat.schedule,
        manifest_id=manifest_id,
        payload={
            "scheduled_by": {"tool": HEARTBEAT, "manifest": manifest_id, "thread_id": thread_id},
            "fresh_thread": True,
        },
        enabled=enabled,
        created_by=person,
        created_auth=recorded,
        # Waits for its schedule: turning it on is not a request to run it now.
        first_run_at=parse_schedule(heartbeat.schedule).next_after(now),
    )


async def keep_schedule_current(
    settings: Settings, tenant_id: str, job: dict[str, Any], schedule: str
) -> None:
    """Move the job to the manifest's schedule when they differ, due at its next slot from now: an
    operator's edit applies from the next firing, and a schedule someone re-saved over `PUT /jobs`
    lasts one firing. Conditional on the schedule this firing read (`jobs_store.set_schedule`), so a
    heartbeat deleted, disabled or re-saved meanwhile is left exactly as it now is."""
    from felix.jobs import store as jobs_store
    from felix.jobs.schedule import parse_schedule
    from felix.jobs.scheduler import now_ms

    if job.get("schedule") == schedule:
        return
    try:
        await jobs_store.set_schedule(
            settings,
            tenant_id,
            str(job["name"]),
            schedule=schedule,
            next_run_at=parse_schedule(schedule).next_after(now_ms()),
            expected_schedule=str(job.get("schedule") or ""),
        )
    except Exception:
        logger.warning("heartbeat schedule refresh failed", exc_info=True)


@dataclass(slots=True)
class Firing:
    """What a job's firing does differently when the job is a heartbeat: its prompt, the manifest
    it runs, what happens after, and what its run records. `refusal`, when set, is the run's result
    and nothing is invoked."""

    settings: Any
    tenant_id: str
    job: dict[str, Any]
    heartbeat: HeartbeatSpec | None = None
    prompt: str = ""
    refusal: dict[str, Any] | None = None

    def adjust(self, manifest: Manifest) -> Manifest:
        return manifest if self.heartbeat is None else for_firing(manifest, self.heartbeat)

    async def after(self) -> None:
        """Prune the firing threads past `keep_threads`. Never fails the run."""
        if self.heartbeat is None:
            return
        try:
            await prune_firing_threads(self.settings, self.tenant_id, self.job, self.heartbeat.keep_threads)
        except Exception:
            logger.warning("heartbeat_prune_failed", exc_info=True)

    def result(self, answer: str, extras: dict[str, Any], thread_id: str) -> dict[str, Any]:
        """Quiet when the firing sent no notice: `notify` counts each call it let through on the
        firing's context (`SENT_THIS_RUN_EXTRA`), delivered or not."""
        from felix.push.agent_tool import SENT_THIS_RUN_EXTRA

        return firing_result(answer, sent=int(extras.get(SENT_THIS_RUN_EXTRA) or 0), thread_id=thread_id)


async def _turn_off(settings: Settings, tenant_id: str, job: dict[str, Any], message: str) -> dict[str, Any]:
    """Disable the heartbeat -- first, so a sweep overlapping this one does not fire it again -- and
    the error its run records."""
    from felix.jobs import store as jobs_store

    await jobs_store.disable_job(settings, tenant_id, job["name"])
    return {"status": "error", "error": message}


async def begin_firing(
    settings: Settings, tenant_id: str, job: dict[str, Any], manifest: Manifest
) -> Firing | None:
    """The firing of a heartbeat job against its resolved `manifest`, or None for any other job.

    The prompt is the manifest's checklist as it is now -- never the job's payload, which a holder of
    `jobs:write` could have re-saved. A manifest that no longer defines a heartbeat turns it off.
    One past its `max_firings_per_day` is refused before anything is invoked, recorded `skipped`
    with reason `daily_cap` (`over_daily_cap`), and stays on."""
    if not is_heartbeat_job(job):
        return None
    firing = Firing(settings=settings, tenant_id=tenant_id, job=job)
    heartbeat = manifest.spec.heartbeat
    if heartbeat is None:
        firing.refusal = await _turn_off(settings, tenant_id, job, NO_HEARTBEAT)
        return firing
    await keep_schedule_current(settings, tenant_id, job, heartbeat.schedule)
    firing.refusal = await over_daily_cap(settings, tenant_id, job, heartbeat)
    if firing.refusal is None:
        firing.heartbeat, firing.prompt = heartbeat, firing_prompt(heartbeat.checklist)
    return firing


async def over_daily_cap(
    settings: Settings, tenant_id: str, job: dict[str, Any], heartbeat: HeartbeatSpec
) -> dict[str, Any] | None:
    """The skipped run's result when this heartbeat has already run `max_firings_per_day` times in
    the current UTC day -- scheduled or asked for, every run but a skipped one -- else None, and
    always None with no cap set. The heartbeat stays on: the cap holds a day's firings, it is not a
    fault."""
    from felix.jobs import store as jobs_store
    from felix.jobs.scheduler import now_ms

    if heartbeat.max_firings_per_day is None:
        return None
    since = jobs_store.utc_day_start(now_ms())
    ran = await jobs_store.count_runs_since(settings, tenant_id, str(job["name"]), since)
    if ran < heartbeat.max_firings_per_day:
        return None
    return {
        "status": jobs_store.SKIPPED,
        "reason": DAILY_CAP,
        "max_firings_per_day": heartbeat.max_firings_per_day,
    }


def cap_warnings(manifest: Manifest) -> list[str]:
    """What an author should hear about the heartbeat's cap: one set below what its schedule fires in
    a UTC day holds the rest of that day's slots `skipped`. Allowed -- an operator may want a fast
    cadence early and quiet later -- but never silent."""
    import time

    from felix.jobs.schedule import ScheduleError, most_firings_per_utc_day, parse_schedule

    heartbeat = manifest.spec.heartbeat
    if heartbeat is None or heartbeat.max_firings_per_day is None:
        return []
    try:
        most = most_firings_per_utc_day(parse_schedule(heartbeat.schedule), int(time.time() * 1000))
    except ScheduleError:
        return []
    if most is None or heartbeat.max_firings_per_day >= most:
        return []
    return [
        f"spec.heartbeat.max_firings_per_day is {heartbeat.max_firings_per_day}, and its schedule fires "
        f"up to {most} times in a UTC day: every firing past the cap is recorded skipped and runs "
        "nothing until UTC midnight"
    ]


async def manifest_missing(settings: Settings, tenant_id: str, job: dict[str, Any]) -> dict[str, Any] | None:
    """A heartbeat whose manifest no longer resolves is turned off, as one whose manifest dropped
    its heartbeat is; its run records why. None for any other job, whose firing just fails."""
    if not is_heartbeat_job(job):
        return None
    return await _turn_off(settings, tenant_id, job, NO_MANIFEST)


def expired_message(job: dict[str, Any]) -> str | None:
    """The run error of a heartbeat refused because its enrolling credential expired; None for
    another job, which keeps the scheduler's own wording."""
    return CREDENTIAL_EXPIRED if is_heartbeat_job(job) else None


__all__ = [
    "CREDENTIAL_EXPIRED",
    "DAILY_CAP",
    "HEARTBEAT",
    "HEARTBEAT_OK",
    "NO_HEARTBEAT",
    "NO_MANIFEST",
    "Firing",
    "HeartbeatRefused",
    "begin_firing",
    "cap_warnings",
    "enrol",
    "expired_message",
    "firing_prompt",
    "firing_result",
    "for_firing",
    "get_mine",
    "is_heartbeat_job",
    "job_name",
    "manifest_missing",
    "mine",
    "origin",
    "over_daily_cap",
    "prune_firing_threads",
    "public",
]
