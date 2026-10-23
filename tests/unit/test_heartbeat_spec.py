"""`spec.heartbeat`: what a manifest may declare, and the pieces a firing is built from.

The behaviour end to end -- enrolment, the sweep, quiet and eventful firings, retention -- is
`tests/e2e/test_heartbeat.py`. These pin the refusals at manifest write and the pure helpers.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.jobs import heartbeat as hb
from felix.manifests.loader import parse_manifest
from pydantic import ValidationError


def _manifest(heartbeat: dict[str, Any] | None, *, notify: bool = True, **spec: Any) -> Any:
    body: dict[str, Any] = {"pattern": "react", **spec}
    if notify:
        body["notify"] = {}
    if heartbeat is not None:
        body["heartbeat"] = heartbeat
    return parse_manifest(
        {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": "hb"}, "spec": body}
    )


def _refusal(heartbeat: dict[str, Any], **kwargs: Any) -> str:
    with pytest.raises((ValidationError, ValueError)) as caught:
        _manifest(heartbeat, **kwargs)
    return str(caught.value)


def test_the_defaults_are_hourly_on_the_manifest_s_model() -> None:
    spec = _manifest({"checklist": "Check the build."}).spec.heartbeat
    assert (
        spec.schedule,
        spec.min_interval_minutes,
        spec.model,
        spec.max_turns,
        spec.keep_threads,
        spec.max_firings_per_day,
    ) == ("every:60m", 30, None, 6, 20, None), "no cap unless the operator sets one"


def test_a_cap_below_what_the_schedule_could_fire_is_allowed() -> None:
    # Every 15 minutes is 96 a day; a cap of 4 is a configuration, not an error.
    heartbeat = {
        "checklist": "x",
        "schedule": "every:15m",
        "min_interval_minutes": 15,
        "max_firings_per_day": 4,
    }
    assert _manifest(heartbeat).spec.heartbeat.max_firings_per_day == 4


@pytest.mark.parametrize(
    ("schedule", "cap", "warned"),
    [
        ("every:15m", 4, "fires up to 96 times"),
        ("every:60m", 23, "fires up to 24 times"),
        ("every:60m", 24, ""),  # exactly what it fires: the cap never binds
        ("every:60m", None, ""),  # no cap
        ("every:7h", 3, "fires up to 4 times"),  # 24/7 rounds up: some UTC days hold four
        ("0 9,10 * * 1-5", 1, "fires up to 2 times"),  # the busiest day, not a weekend
        ("0 9,10 * * 1-5", 2, ""),
    ],
)
def test_a_cap_below_the_schedule_s_firings_is_a_write_warning(
    schedule: str, cap: int | None, warned: str
) -> None:
    """`manifest_warnings` -- what `PUT /manifests` returns and `felix validate-manifest` prints."""
    from felix.manifests.governance import manifest_warnings

    heartbeat: dict[str, Any] = {"checklist": "x", "schedule": schedule, "min_interval_minutes": 15}
    if cap is not None:
        heartbeat["max_firings_per_day"] = cap
    warnings = [w for w in manifest_warnings(_manifest(heartbeat)) if "max_firings_per_day" in w]
    if not warned:
        assert warnings == []
        return
    (warning,) = warnings
    assert warned in warning and f"max_firings_per_day is {cap}" in warning, warning


def test_a_heartbeat_needs_notify() -> None:
    assert "heartbeat needs spec.notify" in _refusal({"checklist": "Check the build."}, notify=False)


@pytest.mark.parametrize(
    ("schedule", "says"),
    [
        ("at:2030-01-01T09:00:00Z", "must recur"),
        ("", "must recur"),
        ("every:10m", "at most once every 30 minutes"),
        ("*/5 * * * *", "at most once every 30 minutes"),
        ("0 9,9 * * *", ""),  # a cron that fires once a day is fine
        ("whenever", "heartbeat.schedule"),
    ],
)
def test_the_schedule_recurs_no_faster_than_its_floor(schedule: str, says: str) -> None:
    heartbeat = {"checklist": "Check the build.", "schedule": schedule}
    if not says:
        assert _manifest(heartbeat).spec.heartbeat.schedule == schedule
        return
    assert says in _refusal(heartbeat)


def test_the_floor_is_the_operator_s_but_never_under_fifteen_minutes() -> None:
    assert _manifest({"checklist": "x", "schedule": "every:15m", "min_interval_minutes": 15}).spec.heartbeat
    assert "min_interval_minutes" in _refusal(
        {"checklist": "x", "schedule": "every:10m", "min_interval_minutes": 10}
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("checklist", "   "),
        ("checklist", "x" * 4001),
        ("max_turns", 0),
        ("max_turns", 21),
        ("keep_threads", 0),
        ("keep_threads", 201),
        ("max_firings_per_day", 0),
        ("max_firings_per_day", 1441),
    ],
)
def test_out_of_range_fields_are_refused(field: str, value: Any) -> None:
    assert _refusal({"checklist": "Check the build.", field: value})


def test_a_checklist_is_required_and_unknown_keys_are_refused() -> None:
    assert "checklist" in _refusal({})
    assert "prompt" in _refusal({"checklist": "x", "prompt": "y"})


def test_the_job_name_is_one_per_person_per_manifest() -> None:
    name = hb.job_name("assistant", "google:123")
    assert name == hb.job_name("assistant", "google:123")
    assert name != hb.job_name("assistant", "google:124")
    assert name != hb.job_name("helper", "google:123")
    assert name.startswith("hb-assistant-") and len(name) == len("hb-assistant-") + 12
    assert ":" not in name and "google" not in name
    for bad in ("", "a:b", "a/b", "x" * 129):
        with pytest.raises(ValueError):
            hb.job_name(bad, "alice")


def test_a_firing_runs_on_the_heartbeat_s_model_and_step_bound_and_leaves_the_manifest_alone() -> None:
    manifest = _manifest(
        {"checklist": "Check the build.", "model": "claude-haiku", "max_turns": 3},
        model={"id": "claude-sonnet", "temperature": 0.2},
        recursion_limit=12,
    )
    firing = hb.for_firing(manifest, manifest.spec.heartbeat)
    assert (firing.spec.model.id, firing.spec.model.temperature) == ("claude-haiku", 0.2)
    assert (firing.spec.recursion_limit, firing.spec.max_turns) == (3, 3)
    assert (manifest.spec.model.id, manifest.spec.recursion_limit) == ("claude-sonnet", 12)
    unchanged = hb.for_firing(manifest, manifest.spec.heartbeat.model_copy(update={"model": None}))
    assert unchanged.spec.model.id == "claude-sonnet"


@pytest.mark.parametrize(
    ("spec", "want"),
    [
        ({"recursion_limit": 2, "max_turns": 2}, (2, 2)),  # the manifest's own bound is lower
        ({}, (6, 4)),  # unset: the react default (10) and the field's default (4)
        ({"recursion_limit": 40, "max_turns": 40}, (6, 6)),  # the heartbeat's is lower
    ],
)
def test_a_firing_never_runs_further_than_the_manifest_itself_would(
    spec: dict[str, Any], want: tuple[int, int]
) -> None:
    manifest = _manifest({"checklist": "Check the build.", "max_turns": 6}, **spec)
    firing = hb.for_firing(manifest, manifest.spec.heartbeat)
    assert (firing.spec.recursion_limit, firing.spec.max_turns) == want


def test_the_prompt_is_the_checklist_in_the_fixed_frame() -> None:
    prompt = hb.firing_prompt("  Look at the queue.\n")
    assert "--- checklist ---\nLook at the queue.\n--- end of checklist ---" in prompt
    assert f"reply with exactly {hb.HEARTBEAT_OK} and do not call notify" in prompt
    assert "call notify once" in prompt


def test_a_job_is_a_heartbeat_only_when_its_origin_names_the_manifest_it_runs() -> None:
    by = {"tool": "heartbeat", "manifest": "assistant", "thread_id": "default:hb-1"}
    assert hb.is_heartbeat_job({"manifest_id": "assistant", "payload": {"scheduled_by": by}})
    assert not hb.is_heartbeat_job({"manifest_id": "other", "payload": {"scheduled_by": by}})
    assert not hb.is_heartbeat_job(
        {"manifest_id": "assistant", "payload": {"scheduled_by": {**by, "tool": "schedule_task"}}}
    )
    assert not hb.is_heartbeat_job({"manifest_id": "assistant", "payload": {}})


def test_quiet_is_no_notice_sent_and_the_answer_is_cut() -> None:
    assert hb.firing_result("HEARTBEAT_OK", sent=0, thread_id="t")["quiet"] is True
    noisy = hb.firing_result("x" * 900, sent=1, thread_id="t")
    assert noisy["quiet"] is False and len(noisy["answer"]) == hb.MAX_ANSWER_CHARS


# --- against the stores ------------------------------------------------------------------------


@pytest.fixture
def settings() -> Any:
    from tests.support.factories import make_settings

    return make_settings()


async def _thread(settings: Any, thread_id: str) -> None:
    from felix.session.store import get_session_store
    from felix.session.types import AppendableEvent

    await (
        get_session_store(settings, tenant_id="default")
        .open(thread_id)
        .append(AppendableEvent(kind="message", role="user", content="x"))
    )


async def test_pruning_takes_only_threads_a_firing_minted(settings: Any) -> None:
    """Job names are not validated, so a job named `hb-x:other` has threads under `hb-x`'s prefix."""
    from felix.session.prune import threads_under

    mine, theirs = "default:job:hb-x:0123456789abcdef", "default:job:hb-x:other:0123456789abcdef"
    for thread in (mine, theirs):
        await _thread(settings, thread)
    job = {"name": "hb-x", "manifest_id": "m", "payload": {}}
    assert await hb.prune_firing_threads(settings, "default", job, keep=0) == 1
    assert set(await threads_under(settings, tenant_id="default", prefix="default:job:hb-x:")) == {theirs}


async def _heartbeat_job(settings: Any, **kw: Any) -> dict[str, Any]:
    from felix.jobs import store as jobs_store

    payload = {"scheduled_by": {"tool": "heartbeat", "manifest": "m", "thread_id": None}}
    return await jobs_store.put_job(
        settings, "default", "hb-m-1", schedule="every:60m", manifest_id="m", payload=payload, **kw
    )


async def test_a_schedule_refresh_never_brings_back_a_heartbeat_deleted_meanwhile(settings: Any) -> None:
    from felix.jobs import store as jobs_store

    read = await _heartbeat_job(settings)
    await jobs_store.delete_job(settings, "default", "hb-m-1")
    await hb.keep_schedule_current(settings, "default", read, "every:2h")
    assert await jobs_store.get_job(settings, "default", "hb-m-1") is None


async def test_a_schedule_refresh_never_re_enables_a_heartbeat_turned_off_meanwhile(settings: Any) -> None:
    from felix.jobs import store as jobs_store

    read = await _heartbeat_job(settings)
    await _heartbeat_job(settings, enabled=False)
    await hb.keep_schedule_current(settings, "default", read, "every:2h")
    job = await jobs_store.get_job(settings, "default", "hb-m-1")
    assert job is not None and (job["enabled"], job["schedule"]) == (False, "every:2h")


def test_only_keys_and_felix_sign_ins_are_re_checked_at_each_firing() -> None:
    import json

    from felix.jobs.scheduler import rechecks_at_firing

    from tests.support.factories import make_settings

    google = make_settings(
        google_accounts=json.dumps({"dana@example.com": {"tenant": "acme", "scopes": ["heartbeats:write"]}})
    )
    signin = {"scheme": "self", "idp": "google", "email": "dana@example.com"}
    assert rechecks_at_firing(make_settings(), "acme", "dana", {"scheme": "api_key"})
    assert rechecks_at_firing(google, "acme", "google:1", signin)
    # A personal GitHub tenant has no grant to re-read; another issuer's token has no record at all.
    assert not rechecks_at_firing(make_settings(), "gh-42", "github:42", {"scheme": "self", "idp": "github"})
    assert not rechecks_at_firing(make_settings(), "acme", "okta|dana", {"scheme": "jwt"})


def test_a_task_its_writer_moved_to_another_manifest_is_no_longer_swept_as_a_task() -> None:
    """`is_fired_task` reads `scheduled_by` through `jobs_store.scheduled_by`, which counts a record
    only on the manifest it names: a task re-saved onto another manifest is an ordinary job now,
    and retention leaves it."""
    from felix.jobs.agent_tools import is_fired_task

    by = {"tool": "schedule_task", "manifest": "assistant", "thread_id": None}
    fired = {"enabled": False, "last_run_at": 1, "schedule": "every:60m", "payload": {"scheduled_by": by}}
    assert is_fired_task({**fired, "manifest_id": "assistant"})
    assert not is_fired_task({**fired, "manifest_id": "other"})
