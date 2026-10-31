"""`GET /health/worker`: the worker's heartbeat, what a deploy waits on after `/ready`.

0.19.0 rolled with a worker that refused its config at startup while `/health` and `/ready`, which
only the API answers, stayed green. The beat is sent only once startup passed, with the version, so
these drive the real startup hook as well as the route.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from felix import worker_beat

from tests.support.factories import app_client, make_settings

NOW = 1_800_000_000_000


class _FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def set(self, key: str, value: str) -> bool:
        self.data[key] = value
        return True


class _Conn:
    """Stands in for the beat's `RedisConnection`: `get` hands back `client` (None: unreachable)."""

    def __init__(self, client: Any) -> None:
        self.client = client

    async def get(self) -> Any:
        return self.client

    async def fallback(self, _what: str) -> None:
        return None


@pytest.fixture(autouse=True)
def _no_redis_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    # `record_beat` always asks the module's connection, which reads the process's own settings
    # (scripts/test.sh points them at a closed port): a real connect per test, and a back-off
    # latched for whichever test runs next. Tests that want a Redis patch in a fake over this.
    monkeypatch.setattr(worker_beat, "_conn", _Conn(None))


def _no_redis(**kw: Any) -> Any:
    return make_settings(redis_url="", **kw)


async def test_the_route_answers_never_then_ok_then_stale_with_the_version(monkeypatch: Any) -> None:
    clock = [NOW]
    monkeypatch.setattr(worker_beat, "now_ms", lambda: clock[0])
    async with app_client(_no_redis(worker_stale_seconds=60)) as client:
        never = await client.get("/health/worker")
        await worker_beat.record_beat(worker_beat.Beat(NOW, "1.2.3", "b0"))
        clock[0] = NOW + 60_000
        fresh = await client.get("/health/worker")
        clock[0] = NOW + 61_000
        stale = await client.get("/health/worker")

    assert (never.status_code, never.json()) == (
        503,
        {"status": "never", "last_beat_age_s": None, "stale_after_s": 60, "version": None, "boot": None},
    )
    assert (fresh.status_code, fresh.json()) == (
        200,
        {"status": "ok", "last_beat_age_s": 60, "stale_after_s": 60, "version": "1.2.3", "boot": "b0"},
    )
    assert (stale.status_code, stale.json()["status"], stale.json()["version"]) == (503, "stale", "1.2.3")


async def test_a_beat_from_a_clock_ahead_reads_as_age_zero(monkeypatch: Any) -> None:
    monkeypatch.setattr(worker_beat, "now_ms", lambda: NOW)
    await worker_beat.record_beat(worker_beat.Beat(NOW + 5_000, "1.2.3", "b0"))
    health = await worker_beat.worker_health(_no_redis())
    assert (health.status, health.last_beat_age_s) == ("ok", 0)


async def test_the_route_needs_no_credential() -> None:
    settings = _no_redis(auth_mode="api_key", auth_api_keys=json.dumps({"k": {"tenant_id": "t"}}))
    async with app_client(settings) as client:
        answer = await client.get("/health/worker")
    assert answer.status_code == 503, answer.text
    assert answer.json()["status"] == "never"


async def test_a_redis_that_cannot_be_read_is_unknown_not_never() -> None:
    async with app_client(make_settings(redis_url="redis://127.0.0.1:9/0")) as client:
        answer = await client.get("/health/worker")
    assert (answer.status_code, answer.json()["status"]) == (503, "unknown")


async def test_the_beat_crosses_processes_through_redis(monkeypatch: Any) -> None:
    """The worker writes, the API reads: with this process's own copy cleared, Redis alone answers."""
    fake = _FakeRedis()
    monkeypatch.setattr(worker_beat, "_conn", _Conn(fake))
    settings = make_settings(redis_url="redis://127.0.0.1:9/0")
    beat = worker_beat.Beat(NOW, "1.2.3", "b0")

    await worker_beat.record_beat(beat)
    worker_beat.reset_beat_for_tests()
    assert await worker_beat.read_beat(settings) == (beat, False)

    # This process's own older copy does not hide a newer beat another process wrote.
    await worker_beat.record_beat(worker_beat.Beat(NOW - 1, "1.2.2", "old"))
    fake.data[worker_beat.BEAT_KEY] = json.dumps({"at": NOW, "version": "1.2.3", "boot": "b0"})
    assert await worker_beat.read_beat(settings) == (beat, False)

    # A value this version cannot read is no beat, not a crash on a public route.
    worker_beat.reset_beat_for_tests()
    fake.data[worker_beat.BEAT_KEY] = "not json"
    assert await worker_beat.read_beat(settings) == (None, False)


async def test_beat_until_keeps_beating_under_one_boot_and_stops_when_told(monkeypatch: Any) -> None:
    """A worker that beat once would read stale a minute later; one that drew a new boot each beat
    would look restarted to every roll."""
    clock = [NOW]
    monkeypatch.setattr(worker_beat, "now_ms", lambda: clock[0])
    monkeypatch.setattr(worker_beat, "BEAT_INTERVAL_S", 0.01)
    stop = asyncio.Event()
    task = asyncio.create_task(worker_beat.beat_until("1.2.3", stop))
    await asyncio.sleep(0)
    first, _ = await worker_beat.read_beat(_no_redis())
    assert first is not None and (first.at, first.version) == (NOW, "1.2.3"), "the first beat is not delayed"

    clock[0] = NOW + 15_000
    for _ in range(100):
        await asyncio.sleep(0.01)
        second, _ = await worker_beat.read_beat(_no_redis())
        if second is not None and second.at > NOW:
            break
    assert second is not None and second.at == NOW + 15_000, "it beat again"
    assert second.boot == first.boot and len(first.boot) == 16
    stop.set()
    await asyncio.wait_for(task, timeout=1)


async def _start_worker(monkeypatch: pytest.MonkeyPatch, settings: Any) -> Any:
    import felix.secrets
    import felix_worker.tasks as tasks

    async def _no_secrets(_settings: Any) -> None:
        return None

    monkeypatch.setattr(tasks, "_settings", settings)
    monkeypatch.setattr(felix.secrets, "hydrate_secrets", _no_secrets)
    for name in ("_start_fiber_loop", "_register_plugin_cron_tasks", "_start_metrics_server"):
        monkeypatch.setattr(tasks, name, lambda: None)
    return tasks


async def test_a_started_worker_beats_its_version_and_stops_on_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from felix import __version__

    tasks = await _start_worker(monkeypatch, _no_redis())
    await tasks._on_worker_startup(None)
    try:
        await asyncio.sleep(0)
        async with app_client(_no_redis()) as client:
            answer = await client.get("/health/worker")
        assert (answer.status_code, answer.json()["version"]) == (200, __version__)
    finally:
        await tasks._on_worker_shutdown(None)
    assert tasks._beat is None


async def test_a_worker_whose_startup_refuses_never_beats(monkeypatch: pytest.MonkeyPatch) -> None:
    """The 0.19.0 worker: its config refused at startup, so nothing may say it is up."""
    refused = _no_redis()
    tasks = await _start_worker(monkeypatch, refused)

    def _refuse(**_kw: Any) -> None:
        raise RuntimeError("refused")

    object.__setattr__(refused, "validate_runtime", _refuse)
    with pytest.raises(RuntimeError, match="refused"):
        await tasks._on_worker_startup(None)
    assert tasks._beat is None
    assert (await worker_beat.read_beat(refused)) == (None, False)
