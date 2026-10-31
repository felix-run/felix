"""CLI doctor + scheduler entrypoint smoke."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner


def test_doctor_memory_ok(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FELIX_DATABASE_URL", "memory://doctor")
    monkeypatch.setenv("FELIX_OBJECT_STORE", "memory")
    monkeypatch.setenv("FELIX_AUTH_MODE", "none")
    monkeypatch.setenv("FELIX_ALLOW_INSECURE", "true")
    monkeypatch.setenv("FELIX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("FELIX_WAREHOUSE", "none")
    # Unreachable redis is expected FAIL — doctor should still exit non-zero or ok
    # depending on redis; use fakeredis URL won't work. Accept exit 1 if redis down.
    from felix.config import get_settings

    get_settings.cache_clear()

    from felix_cli.main import app

    runner = CliRunner()
    result = runner.invoke(app, ["doctor"])
    # Database/object_store/warehouse should pass; redis may fail locally.
    assert "Felix doctor" in result.output
    assert "object_store" in result.output
    get_settings.cache_clear()


def test_scheduler_entrypoint_importable() -> None:
    from felix_worker.main import scheduler_main

    assert callable(scheduler_main)


def test_plugin_cron_register_helper() -> None:
    from felix_worker import tasks as worker_tasks

    # Should not raise when no plugins are installed.
    worker_tasks._register_plugin_cron_tasks()


def _doctor(tmp_path: Path, monkeypatch, redis_url: str) -> str:
    for key, value in {
        "FELIX_DATABASE_URL": "memory://doctor",
        "FELIX_OBJECT_STORE": "memory",
        "FELIX_AUTH_MODE": "none",
        "FELIX_ALLOW_INSECURE": "true",
        "FELIX_DATA_DIR": str(tmp_path),
        "FELIX_WAREHOUSE": "none",
        "FELIX_REDIS_URL": redis_url,
    }.items():
        monkeypatch.setenv(key, value)
    from felix.config import get_settings

    get_settings.cache_clear()
    from felix_cli.main import app

    try:
        return CliRunner().invoke(app, ["doctor"]).output
    finally:
        get_settings.cache_clear()


def test_doctor_says_the_heartbeat_is_not_visible_without_redis(tmp_path: Path, monkeypatch) -> None:
    assert "and worker heartbeats not visible from this process" in _doctor(tmp_path, monkeypatch, "")


def test_doctor_notes_a_stale_scheduler_heartbeat(tmp_path: Path, monkeypatch) -> None:
    from felix.jobs import tick

    async def stale(_settings) -> tick.SchedulerHealth:
        return tick.SchedulerHealth("stale", 901, 300)

    monkeypatch.setattr(tick, "scheduler_health", stale)
    output = _doctor(tmp_path, monkeypatch, "redis://127.0.0.1:9/0")
    assert "scheduler heartbeat stale — last sweep 901s ago" in output


def test_doctor_reports_the_worker_heartbeat(tmp_path: Path, monkeypatch) -> None:
    from felix import worker_beat
    from felix.jobs import tick

    answers = iter(
        [
            worker_beat.WorkerHealth("ok", 4, 60, "1.2.3", "b0"),
            worker_beat.WorkerHealth("stale", 75, 60, "1.2.3", "b0"),
            worker_beat.WorkerHealth("never", None, 60, None, None),
        ]
    )

    async def health(_settings) -> worker_beat.WorkerHealth:
        return next(answers)

    async def scheduler_ok(_settings) -> tick.SchedulerHealth:
        return tick.SchedulerHealth("ok", 1, 300)

    monkeypatch.setattr(worker_beat, "worker_health", health)
    monkeypatch.setattr(tick, "scheduler_health", scheduler_ok)
    beating, stale, never = (_doctor(tmp_path, monkeypatch, "redis://127.0.0.1:9/0") for _ in range(3))
    assert "1.2.3, last beat 4s ago" in beating
    assert "worker heartbeat stale — last beat 75s ago" in stale
    assert "worker heartbeat never — last beat never" in never
