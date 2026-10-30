"""Job health watch: a job that has stopped firing, or keeps failing, is said out loud once.

A job's `last_status` and its run history record a failure, and nobody is told. The
`job_health_watch` sweep (every five minutes, `felix_worker.tasks`) reads every tenant's jobs and
flags two kinds of *incident*:

* **overdue** -- enabled, scheduled (not manual-only), due more than
  `FELIX_JOB_OVERDUE_GRACE_SECONDS` ago, and still due after a scheduler sweep completed
  (`felix.jobs.tick`) two minutes past when the watch first saw it so: the scheduler had its chance
  to fire it and did not. A `running` mark does not exempt it.
* **failing** -- the last `FELIX_JOB_FAILURE_THRESHOLD` runs that ran (skipped ones ignored) were
  all errors.

An incident is recorded on the job (`jobs.health_json`, `store.set_health`, compare-and-set so two
overlapping sweeps open it once), counted (`felix_job_health_incident{kind}`), audited
(`job.health`), and -- when `FELIX_JOB_ALERT_DESTINATION` names a destination open to the job's
tenant (or `"operator": true`) -- posted there once, through the destinations ledger
(`destinations/publish.py`) under the edition `job-health:<job>:<kind>:<since-ms>`. The edition is
fixed when the incident opens, so a sweep that dies between delivering and recording finds the
ledger's row on the next one and does not post twice. An overdue incident closes when the job is no
longer overdue; a failing one once a run since it began succeeded. Disabling a job closes both. A
job failing again after it closed is a new incident, with a new edition, and alerts again.

The alert is the harness's own fixed text: the job's name and tenant (characters outside a plain
set replaced), the kind, since when, and the last error's *class* (`error_class`) -- never
`last_error` itself, which may carry a refusal's own words.

A stopped scheduler stops this sweep too, so the watch for that is outside it: `felix.jobs.tick`
and `GET /health/scheduler`.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from datetime import UTC, datetime
from typing import Any, Literal

from felix.config import Settings
from felix.jobs import store as jobs_store
from felix.logging_setup import loggable

logger = logging.getLogger("felix.jobs.watch")

now_ms = lambda: int(time.time() * 1000)

IncidentKind = Literal["overdue", "failing"]
OVERDUE: IncidentKind = "overdue"
FAILING: IncidentKind = "failing"
KINDS: tuple[IncidentKind, ...] = (OVERDUE, FAILING)

# The ledger's manifest id for the watch's own posts. `:` is outside a manifest name's grammar
# (`manifests/schema.py:MANIFEST_NAME_RE`), so no agent's `post` can take an alert's edition first.
ALERT_MANIFEST = "felix:job-health"
ALERT_EDITION_PREFIX = "job-health"

# What became of an incident's alert, on the incident. Only `pending` is tried again.
PENDING = "pending"
POSTED = "posted"
MAYBE_POSTED = "maybe_posted"
NOT_POSTED = "not_posted"
# No `FELIX_JOB_ALERT_DESTINATION`, or one not open to the job's tenant: nothing is sent. Tried
# again on every sweep (uncounted), so an incident open before routing existed alerts once it does.
UNROUTED = "unrouted"
RETRIED = frozenset({PENDING, UNROUTED})
# A `pending` alert whose destination answered `not_posted` is tried again on each sweep, at most
# this many times in all (an hour of sweeps), then left `not_posted`.
MAX_ALERT_ATTEMPTS = 12
# `health_json["overdue_seen_at"]`: when the watch first saw the job past its due time and grace.
# Overdue opens only once a scheduler sweep completed this long after that (`felix.jobs.tick`): a
# sweep that should have fired the job and did not. Two ticks, so a job re-enabled with yesterday's
# due time, given its first schedule, or due through a scheduler outage is fired before it is
# flagged. Not a kind: no alert, no count.
OVERDUE_SEEN_AT = "overdue_seen_at"
SWEEP_CHANCE_MS = 120_000

# How many runs one check reads to find the threshold's worth that ran: skipped ones (a heartbeat
# over its daily cap) are records, not runs, and may sit between them.
_RUNS_READ_FACTOR = 10
_RUNS_READ_MIN = 50
_PLAIN_NAME = re.compile(r"[A-Za-z0-9._-]{1,64}")
# No space: a name is shown in backticks, and must read as one token.
_SHOWN_CHARS = re.compile(r"[^A-Za-z0-9._:@-]")
# The hashed slug's marker. `:` is outside `_PLAIN_NAME`, so a plain name can never read as one.
_HASHED = "sha:"


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def shown(value: str, *, limit: int = 64) -> str:
    """A job or tenant name as an alert shows it: in backticks, plain characters only, each other one
    a `?`, cut to `limit`. Names are unvalidated path segments; the alert is read in someone's
    channel."""
    return f"`{_SHOWN_CHARS.sub('?', value)[:limit]}`"


def edition(job_name: str, kind: str, since: int) -> str:
    """The incident's edition: stable for its life, new for the next incident. A name outside the
    edition grammar is hashed, behind a marker no plain name can hold."""
    slug = (
        job_name
        if _PLAIN_NAME.fullmatch(job_name)
        else _HASHED + hashlib.sha256(job_name.encode()).hexdigest()[:24]
    )
    return f"{ALERT_EDITION_PREFIX}:{slug}:{kind}:{since}"


def error_class(job: dict[str, Any]) -> str:
    """The class of the job's last error, from the values the harness writes -- never the text.

    `last_error` is a written message, but some of it is a relayed refusal's own words (the scopes
    it lacked, a workflow's name); an alert posted outside the tenant carries none of it."""
    from felix.jobs import heartbeat as hb
    from felix.jobs import scheduler
    from felix.jobs.schedule import ScheduleError, parse_schedule

    error = str(job.get("last_error") or "")
    if not error:
        return "none"
    try:
        parse_schedule(str(job.get("schedule") or ""))
    except ScheduleError:
        return "invalid_schedule"
    known = {
        scheduler.CANCELLED: "cancelled",
        scheduler.EXECUTION_FAILED: "execution_failed",
        scheduler.CREDENTIAL_EXPIRED: "credential_expired",
        hb.CREDENTIAL_EXPIRED: "credential_expired",
        jobs_store.DISABLED_WHILE_RUNNING: "disabled_while_running",
    }
    if error in known:
        return known[error]
    # `felix.errors.internal_error_message`: "internal error", with a request id or without.
    if error == "internal error" or error.startswith("internal error ("):
        return "internal_error"
    return "run_error"


def overdue_since(job: dict[str, Any], *, now: int, grace_ms: int) -> int | None:
    """When the job fell due, if it is past that by more than `grace_ms`; else None. A candidate:
    `watch_job` opens the incident only once a scheduler sweep has had its chance since.

    Not for a disabled job or a manual-only one. A `running` mark does not exempt it: a firing moves
    the due time to the next slot, so a job still due past the grace while marked running is missing
    its schedule -- a run outlasting it, or one killed with its worker (an OOM), whose mark holds the
    job for `RUNNING_STALE_MS`. A recurring job holding no due time is due on the next sweep
    (`store.put_job`), so it is due from its last run, or its writing. A schedule that no longer
    parses still counts: the scheduler refuses it, so it never fires."""
    from felix.jobs.schedule import ScheduleError, parse_schedule

    schedule = str(job.get("schedule") or "").strip()
    if not job.get("enabled"):
        return None
    try:
        parsed = parse_schedule(schedule)
    except ScheduleError:
        parsed = None
    recurring = parsed is not None and not parsed.one_shot
    if recurring and parsed is not None and parsed.next_after(now) is None:
        # Manual only (an empty schedule): it fires when someone asks, never from the sweep.
        return None
    due = job.get("next_run_at")
    if due is None:
        if not recurring:
            # A one-shot with no due time has fired.
            return None
        due = job.get("last_run_at") or job.get("created_at")
    if not isinstance(due, int):
        return None
    return due if due < now - grace_ms else None


def failing_streak(runs: list[dict[str, Any]], threshold: int) -> tuple[int | None, int | None]:
    """`(since, last_ok)`: when the failing streak began -- the oldest of the newest `threshold` runs
    that ran, when all of them errored -- or None; and when the newest successful run started, or
    None. `runs` newest first, as `list_runs` reads them."""
    ran = [r for r in runs if r.get("status") != jobs_store.SKIPPED]
    last_ok = next((int(r["started_at"]) for r in ran if r.get("status") == "ok"), None)
    window = ran[:threshold]
    if len(window) < threshold or any(r.get("status") != "error" for r in window):
        return None, last_ok
    return int(window[-1]["started_at"]), last_ok


def _next_health(
    old: dict[str, Any],
    job: dict[str, Any],
    flagged: dict[str, int],
    last_ok: int | None,
    now: int,
    *,
    scheduler_tick: int | None,
) -> dict[str, Any]:
    """The job's incidents after this sweep: a new one for each kind flagged and not open; an open
    overdue one kept while the job is overdue, and an open failing one until a run since it began
    succeeded -- whether or not this sweep still sees the streak (a raised threshold hides it) and
    even if the job failed again before this sweep looked. A disabled job holds none.

    An overdue candidate opens only once the scheduler's last completed sweep (`scheduler_tick`)
    is `SWEEP_CHANCE_MS` past when the watch first saw it (`OVERDUE_SEEN_AT`); until then the
    sighting is kept, and it goes when the job is no longer due."""
    if not job.get("enabled"):
        return {}
    out: dict[str, Any] = {}
    if OVERDUE in flagged and not old.get(OVERDUE):
        seen_at = old.get(OVERDUE_SEEN_AT)
        seen_at = seen_at if isinstance(seen_at, int) else now
        if scheduler_tick is None or scheduler_tick < seen_at + SWEEP_CHANCE_MS:
            out[OVERDUE_SEEN_AT] = seen_at
            flagged = {k: v for k, v in flagged.items() if k != OVERDUE}
    for kind in KINDS:
        held = old.get(kind)
        if isinstance(held, dict) and held:
            stays = (
                (last_ok is None or last_ok < int(held.get("since") or 0))
                if kind == FAILING
                else kind in flagged
            )
            if stays:
                out[kind] = held
                continue
        # Closed or never open: a kind flagged now is a new incident -- in the same sweep that
        # closed the last one, when the job succeeded and then failed a streak's worth again.
        if kind in flagged:
            out[kind] = {
                "since": flagged[kind],
                "opened_at": now,
                "error_class": error_class(job),
                "alert": PENDING,
                "attempts": 0,
            }
    return out


async def watch_job(
    settings: Settings, tenant_id: str, job: dict[str, Any], *, now: int, scheduler_tick: int | None
) -> dict[str, int]:
    """One job's check: open, keep or close its incidents, then try each pending alert.
    `scheduler_tick` is when the scheduler's last sweep completed (`felix.jobs.tick`), or None."""
    counts = {"opened": 0, "cleared": 0, "alerted": 0}
    flagged: dict[str, int] = {}
    overdue = overdue_since(job, now=now, grace_ms=settings.job_overdue_grace_seconds * 1000)
    if overdue is not None:
        flagged[OVERDUE] = overdue
    threshold = settings.job_failure_threshold
    runs = await jobs_store.list_runs(
        settings, tenant_id, job["name"], limit=max(_RUNS_READ_MIN, threshold * _RUNS_READ_FACTOR)
    )
    # Read for a disabled job too: its streak still shows, and `_next_health` is what closes it.
    failing, last_ok = failing_streak(runs, threshold)
    if failing is not None:
        flagged[FAILING] = failing
    old = dict(job.get("health") or {})
    health = _next_health(old, job, flagged, last_ok, now, scheduler_tick=scheduler_tick)
    if health != old:
        if not await jobs_store.set_health(settings, tenant_id, job["name"], seen=old, health=health):
            # Another sweep moved it first, and opened, counted and alerted what it found.
            return counts
        # By identity: `_next_health` keeps an open incident as the very object it read, so a kind
        # whose incident is another object closed one, opened one, or (closed and reopened) both.
        for kind in KINDS:
            if kind in old and health.get(kind) is not old[kind]:
                counts["cleared"] += 1
                _record(settings, tenant_id, job, kind, old[kind], "cleared")
            if kind in health and health[kind] is not old.get(kind):
                counts["opened"] += 1
                _record(settings, tenant_id, job, kind, health[kind], "open")
    counts["alerted"] = await _alert_pending(settings, tenant_id, job, health)
    return counts


def _record(
    settings: Settings, tenant_id: str, job: dict[str, Any], kind: str, incident: dict[str, Any], status: str
) -> None:
    from felix.audit.emit import record_offline_event
    from felix.auth.actors import CRON
    from felix.observability.metrics import record_counter

    if status == "open":
        record_counter("felix_job_health_incident", {"kind": kind})
        logger.warning(
            "job_health_incident tenant=%s job=%s kind=%s",
            loggable(tenant_id, limit=64),
            loggable(str(job["name"]), limit=200),
            kind,
        )
    record_offline_event(
        settings,
        tenant_id,
        "job.health",
        principal=CRON,
        status=status,
        manifest_id=str(job.get("manifest_id") or ""),
        payload={
            "job": str(job["name"]),
            "kind": kind,
            "since": incident.get("since"),
            "error_class": incident.get("error_class"),
        },
    )


def alert_text(
    tenant_id: str, job: dict[str, Any], kind: str, incident: dict[str, Any], threshold: int
) -> tuple[str, str]:
    """The alert's title and text: fixed wording around the harness's own values."""
    name = shown(str(job["name"]))
    since = _iso(int(incident["since"]))
    what = (
        f"was due at {since} and has not fired"
        if kind == OVERDUE
        else f"has failed its last {threshold} runs, the first at {since}"
    )
    clears = "fires" if kind == OVERDUE else "succeeds"
    title = f"Felix job {kind}: {name}"
    text = (
        f"Job {name} in tenant {shown(tenant_id)} {what}. "
        f"Last error: {incident.get('error_class') or 'none'}. "
        f"This alert is sent once per incident; it clears when the job next {clears} or is disabled."
    )
    return title, text


async def _alert_pending(
    settings: Settings, tenant_id: str, job: dict[str, Any], health: dict[str, Any]
) -> int:
    """Try each incident whose alert is still pending; record what became of it. How many posted."""
    posted = 0
    updated = dict(health)
    for kind in KINDS:
        if kind not in health or health[kind].get("alert") not in RETRIED:
            continue
        incident = updated[kind] = dict(health[kind])
        incident["alert"] = await _send(settings, tenant_id, job, kind, incident)
        if incident["alert"] == PENDING:
            incident["attempts"] = int(incident.get("attempts") or 0) + 1
            if incident["attempts"] >= MAX_ALERT_ATTEMPTS:
                incident["alert"] = NOT_POSTED
        posted += incident["alert"] == POSTED
    if updated != health:
        # Lost to another sweep: the ledger holds the delivery, so its next read records it.
        await jobs_store.set_health(settings, tenant_id, job["name"], seen=health, health=updated)
    return posted


async def _send(
    settings: Settings, tenant_id: str, job: dict[str, Any], kind: str, incident: dict[str, Any]
) -> str:
    """Post one incident's alert; the alert state it leaves. `pending` means try again next sweep."""
    from felix.destinations import DestinationError, EditionKey, Post, resolve_harness_destination
    from felix.destinations.publish import publish

    name = settings.job_alert_destination.strip()
    if not name:
        return UNROUTED
    try:
        dest = resolve_harness_destination(settings, tenant_id, name)
    except DestinationError:
        logger.info(
            "job_health_alert_unrouted tenant=%s destination not open to it", loggable(tenant_id, limit=64)
        )
        return UNROUTED
    key = EditionKey(tenant_id, name, ALERT_MANIFEST, edition(str(job["name"]), kind, int(incident["since"])))
    title, text = alert_text(tenant_id, job, kind, incident, settings.job_failure_threshold)
    post = Post(
        edition=key.edition,
        text=dest.apply_links(text),
        title=dest.apply_links(title),
        manifest_id=ALERT_MANIFEST,
        key=key.post_key,
        unattended=True,
    )
    try:
        outcome = await publish(settings, key, dest, post, thread_id="")
    except Exception:
        logger.warning(
            "job_health_alert ledger unavailable tenant=%s", loggable(tenant_id, limit=64), exc_info=True
        )
        return PENDING
    if outcome.status in ("posted", "already_posted"):
        return POSTED
    if outcome.status in ("not_posted", "in_flight"):
        # Certainly not delivered, or another sweep delivering it now: read again next sweep.
        return PENDING
    return MAYBE_POSTED


async def run_job_health_sweep(
    settings: Settings, *, tenant_id: str, now: int | None = None, scheduler_tick: Any = ...
) -> dict[str, int]:
    """Check every job of one tenant. Counts of jobs checked and incidents opened, cleared, alerted.
    `scheduler_tick` defaults to the heartbeat's stamp, read once."""
    from felix.jobs.tick import last_tick

    at = now_ms() if now is None else now
    tick = await last_tick(settings) if scheduler_tick is ... else scheduler_tick
    totals = {"jobs": 0, "opened": 0, "cleared": 0, "alerted": 0}
    for job in await jobs_store.list_jobs(settings, tenant_id):
        # A disabled job with nothing open has nothing to check.
        if not job.get("enabled") and not job.get("health"):
            continue
        totals["jobs"] += 1
        try:
            counts = await watch_job(settings, tenant_id, job, now=at, scheduler_tick=tick)
        except Exception:
            logger.exception("job_health_check_failed job=%s", loggable(str(job.get("name")), limit=200))
            continue
        for key, value in counts.items():
            totals[key] += value
    return totals


async def run_job_health_sweep_all_tenants(settings: Settings) -> dict[str, int]:
    """`run_job_health_sweep` for every tenant with jobs, each bound for RLS, as the scheduler's
    own sweep is (`scheduler.run_due_jobs_all_tenants`)."""
    from felix.db.session import rls_tenant
    from felix.jobs.tick import last_tick

    totals = {"jobs": 0, "opened": 0, "cleared": 0, "alerted": 0}
    at = now_ms()
    tick = await last_tick(settings)
    for tenant_id in await jobs_store.list_tenants_with_jobs(settings):
        try:
            with rls_tenant(tenant_id):
                counts = await run_job_health_sweep(
                    settings, tenant_id=tenant_id, now=at, scheduler_tick=tick
                )
        except Exception:
            # One tenant's bad row must not stop the watch for everyone else.
            logger.exception("job_health_sweep_failed tenant=%s", loggable(tenant_id, limit=64))
            continue
        for key, value in counts.items():
            totals[key] += value
    return totals


__all__ = [
    "ALERT_MANIFEST",
    "FAILING",
    "KINDS",
    "MAX_ALERT_ATTEMPTS",
    "OVERDUE",
    "IncidentKind",
    "alert_text",
    "edition",
    "error_class",
    "failing_streak",
    "overdue_since",
    "run_job_health_sweep",
    "run_job_health_sweep_all_tenants",
    "shown",
    "watch_job",
]
