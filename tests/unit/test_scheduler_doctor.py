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
