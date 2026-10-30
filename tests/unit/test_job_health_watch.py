"""The job health watch: what it flags, that it reports each incident once, and what it posts.

`felix.jobs.watch` over the `memory://` jobs store and a scripted destination kind registered
through the real seam, so the ledger, the destination registry and the publish path are the
production ones. The scheduler's heartbeat (`felix.jobs.tick`) and its route are at the end.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
from felix import destinations as D
from felix.destinations import Delivery, Destination, DestinationError, Post, ledger
from felix.destinations.tool import EDITION_PATTERN
from felix.jobs import store as jobs_store
from felix.jobs import watch
from felix.run_sources import RequiredSourceUnavailable
from prometheus_client import REGISTRY

from tests.support.factories import app_client, make_settings

TENANT = "acme"
JOB = "nightly"
NOW = 1_800_000_000_000
MINUTE = 60_000
DAY = 86_400_000
GRACE = 900_000
STREAK_START = NOW - 100 * MINUTE
# What a relayed refusal's own words could be; never in an alert.
PLANTED = "inbound refused: missing scope secret-workflow-zeta"


class _Scripted:
    """A destination kind recording each delivery and answering the next scripted outcome."""

    def __init__(self) -> None:
        self.outcomes: list[Delivery] = []
        self.delivered: list[Post] = []

    def validate(self, settings: Any, tenant_id: str, dest: Destination) -> None:
        return None

    async def deliver(self, settings: Any, tenant_id: str, dest: Destination, post: Post) -> Delivery:
        self.delivered.append(post)
        return self.outcomes.pop(0) if self.outcomes else Delivery("posted", ref=f"m{len(self.delivered)}")


@pytest.fixture
def kind() -> Any:
    k = _Scripted()
    D.register_destination_kind("scripted", k)
    try:
        yield k
    finally:
        D._kinds.pop("scripted", None)


def _settings(tenants: list[str] | None = None, *, operator: bool = False, **kw: Any) -> Any:
    entry: dict[str, Any] = (
        {"kind": "scripted", "operator": True}
        if operator
        else {
            "kind": "scripted",
            "tenants": tenants or [TENANT],
        }
    )
    return make_settings(destinations=json.dumps({"ops": entry}), job_alert_destination="ops", **kw)


async def _job(settings: Any, name: str = JOB, *, schedule: str = "", enabled: bool = True) -> dict[str, Any]:
    return await jobs_store.put_job(
        settings, TENANT, name, schedule=schedule, manifest_id="quick", enabled=enabled
    )


async def _runs(settings: Any, statuses: list[str], *, name: str = JOB, start: int = STREAK_START) -> None:
    for i, status in enumerate(statuses):
        await jobs_store.record_run(settings, TENANT, name, status=status, started_at=start + i * MINUTE)


async def _sweep(settings: Any, now: int = NOW, *, tick: int | None = None) -> dict[str, int]:
    return await watch.run_job_health_sweep(settings, tenant_id=TENANT, now=now, scheduler_tick=tick)


async def _health(settings: Any, name: str = JOB) -> dict[str, Any]:
    job = await jobs_store.get_job(settings, TENANT, name)
    assert job is not None
    return job["health"]


async def _failing(settings: Any) -> None:
    await _job(settings)
    await _runs(settings, ["error"] * 3)


def _incidents(kind_name: str) -> float:
    return REGISTRY.get_sample_value("felix_job_health_incident_total", {"kind": kind_name}) or 0.0


# --- overdue: the candidate ---------------------------------------------------------------------


def _row(**kw: Any) -> dict[str, Any]:
    base = {
        "name": JOB,
        "enabled": True,
        "schedule": "*/5 * * * *",
        "next_run_at": NOW - GRACE - 1,
        "last_status": "ok",
        "last_run_at": NOW - 2 * GRACE,
        "created_at": NOW - 10 * GRACE,
    }
    return base | kw


def test_a_scheduled_job_past_its_due_time_and_grace_is_a_candidate_from_its_due_time() -> None:
    assert watch.overdue_since(_row(), now=NOW, grace_ms=GRACE) == NOW - GRACE - 1


def test_a_job_due_within_the_grace_is_not_overdue() -> None:
    assert watch.overdue_since(_row(next_run_at=NOW - GRACE), now=NOW, grace_ms=GRACE) is None


@pytest.mark.parametrize(
    "row",
    [
        pytest.param(_row(enabled=False), id="disabled"),
        pytest.param(_row(schedule=""), id="manual-only"),
        pytest.param(_row(last_status="running", next_run_at=NOW + 5 * MINUTE), id="running-on-schedule"),
        pytest.param(_row(schedule="at:2026-10-21T09:00:00Z", next_run_at=None), id="one-shot-fired"),
    ],
)
def test_a_job_the_scheduler_is_not_meant_to_fire_now_is_not_overdue(row: dict[str, Any]) -> None:
    assert watch.overdue_since(row, now=NOW, grace_ms=GRACE) is None


def test_a_run_killed_with_its_worker_leaves_a_mark_that_does_not_hide_a_stopped_job() -> None:
    """An OOM-killed run: its `running` mark is recent enough to hold the job for six hours, and
    the job's due time is past the grace. That is overdue, not a run in progress."""
    killed = _row(last_status="running", last_run_at=NOW - 30 * MINUTE, next_run_at=NOW - 2 * GRACE)
    assert watch.overdue_since(killed, now=NOW, grace_ms=GRACE) == NOW - 2 * GRACE


def test_a_new_recurring_job_no_sweep_ever_fired_is_due_from_its_writing() -> None:
    fresh = _row(next_run_at=None, last_run_at=None, last_status="")
    assert watch.overdue_since(fresh, now=NOW, grace_ms=GRACE) == NOW - 10 * GRACE


def test_a_job_whose_schedule_no_longer_parses_is_overdue() -> None:
    assert watch.overdue_since(_row(schedule="every tuesday-ish"), now=NOW, grace_ms=GRACE) == NOW - GRACE - 1


# --- overdue: the scheduler's chance --------------------------------------------------------------


async def _due(settings: Any, due: int) -> None:
    await _job(settings, schedule="*/5 * * * *")
    await jobs_store.touch_run(settings, TENANT, JOB, last_run_at=due - 5 * MINUTE, next_run_at=due)


async def _fired(settings: Any, at: int) -> None:
    await jobs_store.touch_run(settings, TENANT, JOB, last_run_at=at, next_run_at=at + 5 * MINUTE)


async def test_an_overdue_job_opens_once_a_sweep_completed_after_it_was_seen_and_clears_once_it_fires(
    kind: Any,
) -> None:
    settings = _settings()
    await _due(settings, NOW - 2 * GRACE)

    first = await _sweep(settings, NOW, tick=NOW - 30_000)
    assert first["opened"] == 0
    assert (await _health(settings)) == {watch.OVERDUE_SEEN_AT: NOW}

    second = await _sweep(settings, NOW + 5 * MINUTE, tick=NOW + 4 * MINUTE)
    assert second["opened"] == 1
    assert (await _health(settings))["overdue"]["since"] == NOW - 2 * GRACE

    await _fired(settings, NOW + 6 * MINUTE)
    assert (await _sweep(settings, NOW + 10 * MINUTE, tick=NOW + 9 * MINUTE))["cleared"] == 1
    assert [p.edition for p in kind.delivered] == [watch.edition(JOB, "overdue", NOW - 2 * GRACE)]
    assert "clears when the job next fires" in kind.delivered[0].text


async def test_a_job_re_enabled_with_yesterdays_due_time_is_fired_before_it_is_flagged(kind: Any) -> None:
    settings = _settings()
    await _due(settings, NOW - DAY)
    await _job(settings, schedule="*/5 * * * *", enabled=False)
    await _job(settings, schedule="*/5 * * * *", enabled=True)

    assert (await _sweep(settings, NOW, tick=NOW - 30_000))["opened"] == 0
    await _fired(settings, NOW + 30_000)

    assert (await _sweep(settings, NOW + 5 * MINUTE, tick=NOW + 4 * MINUTE))["opened"] == 0
    assert await _health(settings) == {}
    assert kind.delivered == []


async def test_a_job_moved_from_manual_to_daily_is_not_flagged_before_a_sweep_had_its_chance(
    kind: Any, monkeypatch: Any
) -> None:
    settings = _settings()
    monkeypatch.setattr(jobs_store, "now_ms", lambda: NOW - DAY)
    await _job(settings, schedule="")
    await _job(settings, schedule="@daily")

    assert (await _sweep(settings, NOW, tick=NOW - 30_000))["opened"] == 0
    assert set(await _health(settings)) == {watch.OVERDUE_SEEN_AT}


async def test_jobs_due_through_a_scheduler_outage_are_not_flagged_while_it_is_down(kind: Any) -> None:
    """The watch runs (a manual sweep, a second scheduler) while the scheduler that fires jobs has
    not swept for two hours: every due job is the scheduler's fault, which `/health/scheduler` says."""
    settings = _settings()
    await _due(settings, NOW - 60 * MINUTE)

    for at in (NOW, NOW + 5 * MINUTE, NOW + 10 * MINUTE):
        assert (await _sweep(settings, at, tick=NOW - 120 * MINUTE))["opened"] == 0
    assert kind.delivered == []


async def test_no_scheduler_tick_at_all_flags_nothing_overdue(kind: Any) -> None:
    settings = _settings()
    await _due(settings, NOW - 60 * MINUTE)
    await _sweep(settings, NOW, tick=None)
    assert (await _sweep(settings, NOW + 5 * MINUTE, tick=None))["opened"] == 0


# --- failing -------------------------------------------------------------------------------------


def _hist(*statuses: str) -> list[dict[str, Any]]:
    """Runs newest first, as `list_runs` returns them; `started_at` falls with age."""
    return [{"status": s, "started_at": NOW - i * MINUTE} for i, s in enumerate(statuses)]


def test_exactly_the_threshold_of_errors_is_failing_since_the_oldest_of_them() -> None:
    assert watch.failing_streak(_hist("error", "error", "error", "ok"), 3) == (
        NOW - 2 * MINUTE,
        NOW - 3 * MINUTE,
    )


def test_one_error_short_of_the_threshold_is_not_failing() -> None:
    assert watch.failing_streak(_hist("error", "error"), 3) == (None, None)
    assert watch.failing_streak(_hist("error", "ok", "error", "error"), 3) == (None, NOW - MINUTE)


def test_skipped_runs_are_not_runs_and_do_not_break_a_streak() -> None:
    history = _hist("skipped", "error", "skipped", "error", "error")
    assert watch.failing_streak(history, 3) == (NOW - 4 * MINUTE, None)


# --- incidents -----------------------------------------------------------------------------------


async def test_a_failing_job_is_reported_once_across_sweeps(kind: Any) -> None:
    settings = _settings()
    await _failing(settings)
    before = _incidents("failing")

    first = await _sweep(settings)
    await _runs(settings, ["error"], start=NOW - MINUTE)
    second = await _sweep(settings, NOW + 5 * MINUTE)

    assert (first["opened"], first["alerted"]) == (1, 1)
    assert (second["opened"], second["alerted"]) == (0, 0)
    (post,) = kind.delivered
    assert post.edition == watch.edition(JOB, "failing", STREAK_START)
    assert post.manifest_id == watch.ALERT_MANIFEST
    assert (await _health(settings))["failing"]["alert"] == "posted"
    assert _incidents("failing") == before + 1


async def test_a_success_clears_the_incident_and_the_next_streak_alerts_again(kind: Any) -> None:
    settings = _settings()
    await _failing(settings)
    await _sweep(settings)

    await _runs(settings, ["ok"], start=NOW - 50 * MINUTE)
    cleared = await _sweep(settings)
    assert cleared["cleared"] == 1
    assert await _health(settings) == {}

    await _runs(settings, ["error"] * 3, start=NOW - 40 * MINUTE)
    again = await _sweep(settings)

    assert again["opened"] == 1
    assert [p.edition for p in kind.delivered] == [
        watch.edition(JOB, "failing", STREAK_START),
        watch.edition(JOB, "failing", NOW - 40 * MINUTE),
    ]


async def test_an_error_after_the_streak_keeps_the_incident_open(kind: Any) -> None:
    settings = _settings()
    await _failing(settings)
    await _sweep(settings)
    await _runs(settings, ["skipped", "error"], start=NOW - 10 * MINUTE)

    assert (await _sweep(settings))["cleared"] == 0
    assert set(await _health(settings)) == {"failing"}


async def test_a_streak_no_longer_seen_stays_open_until_a_success(kind: Any) -> None:
    """A success *before* the streak, then three errors: open. A raised threshold now reads an `ok`
    in its window and sees no streak -- but nothing succeeded since the incident began."""
    settings = _settings()
    await _job(settings)
    await _runs(settings, ["ok", "error", "error", "error"])
    await _sweep(settings)
    assert set(await _health(settings)) == {"failing"}

    assert (await _sweep(_settings(job_failure_threshold=5)))["cleared"] == 0
    assert set(await _health(settings)) == {"failing"}


async def test_a_success_between_sweeps_clears_the_incident_though_the_job_failed_again(kind: Any) -> None:
    """Between two sweeps the job succeeded once and then failed three times more: the incident the
    first sweep opened is over, and the new streak is a new one, from its own first error."""
    settings = _settings()
    await _failing(settings)
    await _sweep(settings)
    await _runs(settings, ["ok", "error", "error", "error"], start=NOW - 10 * MINUTE)

    counts = await _sweep(settings)

    assert (counts["cleared"], counts["opened"]) == (1, 1)
    assert (await _health(settings))["failing"]["since"] == NOW - 9 * MINUTE
    assert [p.edition for p in kind.delivered] == [
        watch.edition(JOB, "failing", STREAK_START),
        watch.edition(JOB, "failing", NOW - 9 * MINUTE),
    ]


async def test_disabling_a_job_clears_its_incidents(kind: Any) -> None:
    settings = _settings()
    await _failing(settings)
    await _sweep(settings)

    await _job(settings, enabled=False)
    assert (await _sweep(settings))["cleared"] == 1
    assert await _health(settings) == {}


async def test_an_incident_is_audited_when_it_opens_and_when_it_clears(kind: Any) -> None:
    settings = _settings()
    await _failing(settings)
    await _sweep(settings)
    await _runs(settings, ["ok"], start=NOW - MINUTE)
    await _sweep(settings)

    assert await _audited(settings) == [("cleared", "failing", JOB), ("open", "failing", JOB)]


async def _audited(settings: Any) -> list[tuple[str, str, str]]:
    from felix.audit import store as audit_store

    await audit_store.flush_pending(settings)
    rows, _ = await audit_store.query(settings, TENANT, event_type="job.health", limit=10)
    return sorted((r["status"], r["payload_json"]["kind"], r["payload_json"]["job"]) for r in rows)


async def test_a_sweep_overlapping_another_opens_nothing_the_other_opened(kind: Any) -> None:
    """Both read the job before either wrote; the compare-and-set lets one open the incident."""
    settings = _settings()
    await _failing(settings)
    read = await jobs_store.get_job(settings, TENANT, JOB)
    assert read is not None

    first = await watch.watch_job(settings, TENANT, read, now=NOW, scheduler_tick=None)
    second = await watch.watch_job(settings, TENANT, read, now=NOW, scheduler_tick=None)

    assert (first["opened"], second["opened"]) == (1, 0)
    assert len(kind.delivered) == 1


async def test_a_sweep_that_died_after_delivering_does_not_post_again(kind: Any, monkeypatch: Any) -> None:
    """The alert's state is written after the delivery; lose that write and the ledger still has
    the edition, so the next sweep records it posted instead of sending it again."""
    settings = _settings()
    await _failing(settings)
    real = jobs_store.set_health

    async def killed_recording_the_post(*args: Any, health: dict[str, Any], **kw: Any) -> bool:
        if (health.get("failing") or {}).get("alert") == "posted":
            raise RuntimeError("worker killed")
        return await real(*args, health=health, **kw)

    monkeypatch.setattr(jobs_store, "set_health", killed_recording_the_post)
    with pytest.raises(RuntimeError):
        await watch.watch_job(
            settings,
            TENANT,
            await jobs_store.get_job(settings, TENANT, JOB) or {},
            now=NOW,
            scheduler_tick=None,
        )
    assert (await _health(settings))["failing"]["alert"] == "pending"
    monkeypatch.setattr(jobs_store, "set_health", real)

    await _sweep(settings, NOW + 5 * MINUTE)

    assert len(kind.delivered) == 1
    assert (await _health(settings))["failing"]["alert"] == "posted"


# --- routing ------------------------------------------------------------------------------------


async def test_no_destination_set_records_counts_and_audits_the_incident_and_posts_nothing(kind: Any) -> None:
    settings = make_settings()
    await _failing(settings)
    before = _incidents("failing")

    counts = await _sweep(settings)

    assert (counts["opened"], counts["alerted"]) == (1, 0)
    assert (await _health(settings))["failing"]["alert"] == "unrouted"
    assert kind.delivered == []
    assert _incidents("failing") == before + 1
    assert await _audited(settings) == [("open", "failing", JOB)]


async def test_a_destination_not_open_to_the_jobs_tenant_posts_nothing(kind: Any) -> None:
    settings = _settings(tenants=["someone-else"])
    await _failing(settings)

    assert (await _sweep(settings))["opened"] == 1
    assert (await _health(settings))["failing"]["alert"] == "unrouted"
    assert kind.delivered == []


async def test_an_unrouted_incident_alerts_once_routing_exists(kind: Any) -> None:
    await _failing(make_settings())
    await _sweep(make_settings())
    await _sweep(make_settings())
    assert (await _health(make_settings()))["failing"] == {
        **(await _health(make_settings()))["failing"],
        "alert": "unrouted",
        "attempts": 0,
    }

    assert (await _sweep(_settings()))["alerted"] == 1
    assert [p.edition for p in kind.delivered] == [watch.edition(JOB, "failing", STREAK_START)]
    assert (await _health(_settings()))["failing"]["alert"] == "posted"


async def test_an_operator_destination_takes_every_tenants_alerts_and_no_manifest_may_name_it(
    kind: Any,
) -> None:
    settings = _settings(operator=True)
    await _failing(settings)

    assert (await _sweep(settings))["alerted"] == 1
    with pytest.raises(DestinationError, match=r"^unknown destination: ops$"):
        D.resolve_destinations(settings, TENANT, ["ops"])


def test_operator_must_be_a_boolean() -> None:
    raw = json.dumps({"ops": {"kind": "scripted", "operator": "yes"}})
    with pytest.raises(RuntimeError, match="`operator` must be true or false"):
        make_settings(destinations=raw).validate_runtime()


def test_a_tenants_list_holding_star_is_refused_at_boot() -> None:
    raw = json.dumps({"ops": {"kind": "scripted", "tenants": ["*"]}})
    with pytest.raises(RuntimeError, match='write the string "\\*"'):
        make_settings(destinations=raw).validate_runtime()


async def test_an_alert_not_delivered_is_tried_on_the_next_sweep_under_the_same_edition(kind: Any) -> None:
    settings = _settings()
    kind.outcomes = [Delivery("not_posted", detail="refused")]
    await _failing(settings)

    await _sweep(settings)
    assert (await _health(settings))["failing"]["alert"] == "pending"
    await _sweep(settings)

    assert [p.edition for p in kind.delivered] == [watch.edition(JOB, "failing", STREAK_START)] * 2
    assert (await _health(settings))["failing"]["alert"] == "posted"


async def test_an_alert_refused_every_time_stops_at_the_attempt_cap(kind: Any) -> None:
    settings = _settings()
    kind.outcomes = [Delivery("not_posted")] * (watch.MAX_ALERT_ATTEMPTS + 2)
    await _failing(settings)

    for _ in range(watch.MAX_ALERT_ATTEMPTS + 2):
        await _sweep(settings)

    assert len(kind.delivered) == watch.MAX_ALERT_ATTEMPTS
    assert (await _health(settings))["failing"]["alert"] == "not_posted"


async def test_an_edition_another_sweep_is_delivering_is_left_pending(kind: Any) -> None:
    settings = _settings()
    await _failing(settings)
    key = D.EditionKey(TENANT, "ops", watch.ALERT_MANIFEST, watch.edition(JOB, "failing", STREAK_START))
    assert (await ledger.claim(settings, key, thread_id="")).won

    await _sweep(settings)

    assert kind.delivered == []
    assert (await _health(settings))["failing"]["alert"] == "pending"


async def test_a_ledger_that_cannot_be_claimed_leaves_the_alert_pending(kind: Any, monkeypatch: Any) -> None:
    async def down(*_args: Any, **_kw: Any) -> Any:
        raise ConnectionError("database gone")

    settings = _settings()
    await _failing(settings)
    monkeypatch.setattr(ledger, "claim", down)

    await _sweep(settings)

    assert kind.delivered == []
    assert (await _health(settings))["failing"]["alert"] == "pending"


async def test_an_alert_whose_outcome_is_unknown_is_never_sent_again(kind: Any) -> None:
    settings = _settings()
    kind.outcomes = [Delivery("unknown")]
    await _failing(settings)

    await _sweep(settings)
    await _sweep(settings)

    assert len(kind.delivered) == 1
    assert (await _health(settings))["failing"]["alert"] == "maybe_posted"
    row = await ledger.get(
        settings, D.EditionKey(TENANT, "ops", watch.ALERT_MANIFEST, kind.delivered[0].edition)
    )
    assert row is not None and row["status"] == "maybe_posted"


# --- what the alert says ------------------------------------------------------------------------


async def test_the_alert_names_the_error_class_and_never_the_error_text(kind: Any) -> None:
    settings = _settings()
    await _failing(settings)
    await jobs_store.touch_run(
        settings,
        TENANT,
        JOB,
        last_run_at=NOW - MINUTE,
        next_run_at=None,
        last_status="error",
        last_error=PLANTED,
    )

    await _sweep(settings)

    (post,) = kind.delivered
    assert "secret-workflow-zeta" not in post.text + post.title
    assert "Last error: run_error." in post.text
    assert "clears when the job next succeeds" in post.text
    assert post.title == f"Felix job failing: `{JOB}`"


async def test_the_title_goes_through_the_destinations_link_policy_too(kind: Any) -> None:
    settings = _settings()
    await _job(settings, name="www.evil.example")
    await _runs(settings, ["error"] * 3, name="www.evil.example")

    await _sweep(settings)

    (post,) = kind.delivered
    assert "evil.example" not in post.title
    assert "evil.example" not in post.text


@pytest.mark.parametrize(
    ("schedule", "error", "expected"),
    [
        ("", "", "none"),
        ("", "cancelled", "cancelled"),
        ("", "execution failed", "execution_failed"),
        ("", "internal error (request r-1)", "internal_error"),
        ("", "the credential that scheduled this task has expired; schedule it again", "credential_expired"),
        ("", "disabled while marked running", "disabled_while_running"),
        ("every tuesday-ish", "schedule: unparseable", "invalid_schedule"),
        # A schedule the scheduler refuses is why nothing runs, whatever the last error said.
        ("every tuesday-ish", "cancelled", "invalid_schedule"),
        ("", PLANTED, "run_error"),
        # A `required: true` MCP server that could not be used: the class, never the server's name.
        ("", str(RequiredSourceUnavailable("brief", ["tracker"])), "required_source_unavailable"),
    ],
)
def test_an_error_is_classed_from_the_values_the_harness_writes(
    schedule: str, error: str, expected: str
) -> None:
    assert watch.error_class({"schedule": schedule, "last_error": error}) == expected


def test_a_name_outside_the_plain_set_is_shown_masked_and_its_edition_hashed() -> None:
    odd = "job\n<!channel> https://evil.example/x"
    assert watch.shown(odd) == "`job???channel??https:??evil.example?x`"
    assert watch.edition(odd, "failing", 5) == "job-health:sha:e967654f0d4f1f7359a55414:failing:5"
    assert re.fullmatch(EDITION_PATTERN, watch.edition(odd, "failing", 5))


def test_a_plain_name_cannot_spell_a_hashed_edition() -> None:
    """`sha` is a plain name, and its edition has one segment fewer than any hashed one."""
    plain = watch.edition("sha", "failing", 5)
    assert plain == "job-health:sha:failing:5"
    assert plain.count(":") == 3 and watch.edition("a b", "failing", 5).count(":") == 4


async def test_a_resave_of_the_job_keeps_its_open_incident(kind: Any) -> None:
    """`put_job` is an upsert, and the incident is the watch's: re-saving must not re-alert."""
    settings = _settings()
    await _failing(settings)
    await _sweep(settings)

    await _job(settings)
    assert (await _sweep(settings))["opened"] == 0
    assert len(kind.delivered) == 1


# --- settings -----------------------------------------------------------------------------------


def test_an_alert_destination_nobody_registered_fails_the_boot() -> None:
    with pytest.raises(RuntimeError, match="FELIX_JOB_ALERT_DESTINATION"):
        make_settings(job_alert_destination="nowhere").validate_runtime()


def test_a_failure_threshold_longer_than_the_history_kept_fails_the_boot() -> None:
    with pytest.raises(RuntimeError, match="FELIX_JOB_FAILURE_THRESHOLD"):
        make_settings(job_failure_threshold=5, job_runs_keep=4).validate_runtime()


# --- the scheduler's heartbeat -------------------------------------------------------------------


def _no_redis(**kw: Any) -> Any:
    return make_settings(redis_url="", **kw)


async def test_the_scheduler_route_answers_never_then_ok_then_stale(monkeypatch: Any) -> None:
    from felix.jobs import tick

    clock = [NOW]
    monkeypatch.setattr(tick, "now_ms", lambda: clock[0])
    async with app_client(_no_redis(scheduler_stale_seconds=300)) as client:
        never = await client.get("/health/scheduler")
        await tick.record_tick(NOW)
        clock[0] = NOW + 300_000
        fresh = await client.get("/health/scheduler")
        clock[0] = NOW + 301_000
        stale = await client.get("/health/scheduler")

    assert (never.status_code, never.json()["status"], never.json()["last_tick_age_s"]) == (
        503,
        "never",
        None,
    )
    assert (fresh.status_code, fresh.json()) == (
        200,
        {"status": "ok", "last_tick_age_s": 300, "stale_after_s": 300},
    )
    assert (stale.status_code, stale.json()["status"], stale.json()["last_tick_age_s"]) == (503, "stale", 301)


async def test_the_scheduler_route_needs_no_credential() -> None:
    settings = _no_redis(auth_mode="api_key", auth_api_keys=json.dumps({"k": {"tenant_id": "t"}}))
    async with app_client(settings) as client:
        answer = await client.get("/health/scheduler")
    assert answer.status_code == 503, answer.text
    assert answer.json()["status"] == "never"


async def test_a_redis_that_cannot_be_read_is_unknown_not_never(monkeypatch: Any) -> None:
    from felix.jobs import tick

    monkeypatch.setattr(tick, "_conn", _Conn(None))
    async with app_client(make_settings(redis_url="redis://127.0.0.1:9/0")) as client:
        answer = await client.get("/health/scheduler")
    assert (answer.status_code, answer.json()["status"]) == (503, "unknown")


class _Conn:
    """Stands in for the tick's `RedisConnection`: `get` hands back `client` (None: unreachable)."""

    def __init__(self, client: Any) -> None:
        self.client = client

    async def get(self) -> Any:
        return self.client

    async def fallback(self, _what: str) -> None:
        return None


class _FakeRedis:
    """GET and SET, which is all the stamp uses."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def set(self, key: str, value: str) -> bool:
        self.data[key] = value
        return True


async def test_the_stamp_crosses_processes_through_redis_and_never_moves_backwards(monkeypatch: Any) -> None:
    """Another process's copy: the local one cleared, the stamp read back from Redis alone."""
    from felix.jobs import tick

    fake = _FakeRedis()
    monkeypatch.setattr(tick, "_conn", _Conn(fake))
    settings = make_settings(redis_url="redis://127.0.0.1:9/0")

    await tick.record_tick(NOW)
    tick.reset_tick_for_tests()
    assert await tick.read_tick(settings) == (NOW, False)

    await tick.record_tick(NOW - MINUTE)
    tick.reset_tick_for_tests()
    assert fake.data[tick.TICK_KEY] == str(NOW)
    assert await tick.last_tick(settings) == NOW


async def test_a_tick_never_moves_the_local_stamp_backwards() -> None:
    from felix.jobs import tick

    await tick.record_tick(NOW)
    await tick.record_tick(NOW - MINUTE)
    assert await tick.last_tick(_no_redis()) == NOW


async def test_a_sweep_that_failed_for_every_tenant_stamps_nothing_and_raises(monkeypatch: Any) -> None:
    """An image deployed before `felix migrate head`: every tenant's `list_jobs` fails on the missing
    column, nothing fires, and the heartbeat must not say the scheduler is fine."""
    from felix.jobs import scheduler, tick

    settings = _no_redis()
    await _job(settings)

    async def schema_behind(*_args: Any, **_kw: Any) -> int:
        raise RuntimeError("column jobs.health_json does not exist")

    monkeypatch.setattr(scheduler, "run_due_jobs", schema_behind)
    with pytest.raises(RuntimeError, match="every tenant"):
        await scheduler.run_due_jobs_all_tenants(settings)

    assert await tick.last_tick(settings) is None
    async with app_client(settings) as client:
        assert (await client.get("/health/scheduler")).status_code == 503


async def test_a_sweep_that_cannot_list_its_tenants_stamps_nothing(monkeypatch: Any) -> None:
    from felix.jobs import scheduler, tick

    async def down(*_args: Any, **_kw: Any) -> list[str]:
        raise ConnectionError("database gone")

    monkeypatch.setattr(jobs_store, "list_tenants_with_jobs", down)
    with pytest.raises(ConnectionError):
        await scheduler.run_due_jobs_all_tenants(_no_redis())
    assert await tick.last_tick(_no_redis()) is None


async def test_one_tenant_failing_still_stamps_the_sweep(monkeypatch: Any) -> None:
    from felix.jobs import scheduler, tick

    settings = _no_redis()
    await _job(settings)
    await jobs_store.put_job(settings, "other", "fine", schedule="", manifest_id="quick")
    real = scheduler.run_due_jobs

    async def one_broken(s: Any, *, tenant_id: str = "default") -> int:
        if tenant_id == TENANT:
            raise RuntimeError("one bad job")
        return await real(s, tenant_id=tenant_id)

    monkeypatch.setattr(scheduler, "run_due_jobs", one_broken)
    await scheduler.run_due_jobs_all_tenants(settings)
    assert await tick.last_tick(settings) is not None


async def test_one_scheduler_tick_after_the_sighting_is_not_yet_its_chance(kind: Any) -> None:
    """A tick a minute after the watch saw the job due may be the very sweep about to fire it."""
    settings = _settings()
    await _due(settings, NOW - 2 * GRACE)
    await _sweep(settings, NOW, tick=NOW - 30_000)

    assert (await _sweep(settings, NOW + 5 * MINUTE, tick=NOW + MINUTE))["opened"] == 0
    assert (await _sweep(settings, NOW + 10 * MINUTE, tick=NOW + 2 * MINUTE + 1))["opened"] == 1
