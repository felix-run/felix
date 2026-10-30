"""A job's cron is read on the clock of the zone the job names, over real HTTP.

Cron was UTC only, so "08:00 every day" for someone in New York was written `0 12 * * *` in summer
and `0 13 * * *` in winter, and a job written either way moved by an hour twice a year. Through the
production app: the job written over `PUT /jobs` with a `timezone`, fired by the worker's sweep
(`run_due_jobs_all_tenants`) with the model scripted, and its next due time read back over `GET`.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from felix.manifests.loader import parse_manifest
from felix_ai.providers.scripted import ScriptedTurn

KEYS = {"sk-e2e-alice": {"tenant_id": "default", "sub": "alice", "scopes": ["jobs:write"]}}
ENV = {"FELIX_AUTH_MODE": "api_key", "FELIX_AUTH_API_KEYS": json.dumps(KEYS)}
ALICE = {"Authorization": "Bearer sk-e2e-alice"}
NEW_YORK = ZoneInfo("America/New_York")
MANIFESTS = {
    "plain": parse_manifest(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": "plain"},
            "spec": {"pattern": "react", "auth": {"inbound": {"allow_anonymous": True}}},
        }
    )
}


def _next_eight_in_new_york(after_ms: int) -> int:
    """08:00 New York time, the first one after `after_ms`, worked out without the cron parser."""
    local = datetime.fromtimestamp(after_ms / 1000, tz=NEW_YORK)
    day = local.date()
    while True:
        candidate = datetime(day.year, day.month, day.day, 8, tzinfo=NEW_YORK)
        if candidate.timestamp() * 1000 > after_ms:
            return int(candidate.timestamp() * 1000)
        day += timedelta(days=1)


async def test_a_zoned_job_is_next_due_at_that_zones_eight_oclock(boot: Any) -> None:
    from felix.jobs.scheduler import run_due_jobs_all_tenants

    body = {
        "schedule": "0 8 * * *",
        "timezone": "America/New_York",
        "manifest_id": "plain",
        "payload": {"prompt": "the morning digest"},
    }
    async with boot([ScriptedTurn(content="done")], env=ENV, manifests=MANIFESTS) as app:
        written = await app.client.put("/jobs/digest", json=body, headers=ALICE)
        assert written.status_code == 200, written.text
        assert written.json()["timezone"] == "America/New_York"
        before = int(time.time() * 1000)
        assert await asyncio.wait_for(run_due_jobs_all_tenants(app.settings), timeout=10) == 1
        after = int(time.time() * 1000)
        job = (await app.client.get("/jobs/digest", headers=ALICE)).json()
        (listed,) = (await app.client.get("/jobs", headers=ALICE)).json()["items"]

    assert job["timezone"] == listed["timezone"] == "America/New_York"
    assert job["next_run_at"] in {_next_eight_in_new_york(before), _next_eight_in_new_york(after)}
    # Never 08:00 UTC: New York is four or five hours behind it, all year.
    assert datetime.fromtimestamp(job["next_run_at"] / 1000, tz=ZoneInfo("UTC")).hour in {12, 13}


async def test_a_zone_that_could_not_move_the_schedule_or_does_not_exist_is_refused(boot: Any) -> None:
    from felix.jobs.schedule import TIMEZONE_NEEDS_CRON

    async with boot([], env=ENV, manifests=MANIFESTS) as app:
        interval = await app.client.put(
            "/jobs/poll",
            json={"schedule": "every:30m", "timezone": "Europe/London", "manifest_id": "plain"},
            headers=ALICE,
        )
        unknown = await app.client.put(
            "/jobs/poll",
            json={"schedule": "0 8 * * *", "timezone": "Mars/Olympus_Mons", "manifest_id": "plain"},
            headers=ALICE,
        )
        missing = await app.client.get("/jobs/poll", headers=ALICE)

    assert (interval.status_code, interval.json()["detail"]) == (422, TIMEZONE_NEEDS_CRON)
    assert unknown.status_code == 422
    assert (
        unknown.json()["detail"] == "timezone: not an IANA zone name, e.g. America/New_York or Europe/London"
    )
    assert missing.status_code == 404, "a refused write stores nothing"


async def test_a_put_that_leaves_the_zone_out_puts_the_job_back_on_utc(
    boot: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The field is replaced like every other on a PUT. On a clock pinned to 2026-10-30T13:00Z
    (09:00 in New York): fired once in New York, the job is next due at 08:00 there on the 31st,
    12:00 UTC; re-saved without a zone it is due at 08:00 UTC that morning, the earlier slot."""
    from felix.jobs import scheduler
    from felix.jobs import store as jobs_store
    from felix.jobs.scheduler import run_due_jobs_all_tenants

    now = 1_793_365_200_000
    monkeypatch.setattr(scheduler, "now_ms", lambda: now)
    monkeypatch.setattr(jobs_store, "now_ms", lambda: now)
    body = {"schedule": "0 8 * * *", "manifest_id": "plain", "payload": {"prompt": "digest"}}
    async with boot([ScriptedTurn(content="done")], env=ENV, manifests=MANIFESTS) as app:
        zoned = await app.client.put(
            "/jobs/poll", json={**body, "timezone": "America/New_York"}, headers=ALICE
        )
        assert zoned.status_code == 200, zoned.text
        assert await asyncio.wait_for(run_due_jobs_all_tenants(app.settings), timeout=10) == 1
        in_new_york = (await app.client.get("/jobs/poll", headers=ALICE)).json()
        utc = await app.client.put("/jobs/poll", json=body, headers=ALICE)

    assert (in_new_york["timezone"], in_new_york["next_run_at"]) == ("America/New_York", 1_793_448_000_000)
    assert utc.status_code == 200, utc.text
    assert (utc.json()["timezone"], utc.json()["next_run_at"]) == (None, 1_793_433_600_000)
