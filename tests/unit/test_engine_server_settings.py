"""Driver, server and task-result bounds are settings, and reach the code that needs them.

The engine had no connect timeout, no statement timeout and no `application_name`; Granian
ran on its defaults with no graceful drain; every cron tick wrote a Taskiq result that
never expired. Each of these is the kind of default that looks fine until the night it
is not — a blackholed host, a runaway query with no owner, a Valkey full of `None`s.
"""

from __future__ import annotations

import pathlib
from typing import Any

import pytest
from felix.config import Settings
from felix.db import session as db_session

PG = "postgresql+psycopg://u:p@localhost:5432/db"


def _settings(**kw: Any) -> Settings:
    return Settings(database_url=PG, **kw)


def test_psycopg_connections_are_bounded_and_named() -> None:
    args = db_session._connect_args(_settings(db_connect_timeout_seconds=7.9, process_role="worker"), PG)
    assert args["connect_timeout"] == 7, "libpq takes whole seconds; 7.9 must not round to 8 or to 0"
    assert args["application_name"] == "felix-worker"
    assert "options" not in args, "no libpq startup options: PgBouncer rejects them"


def test_the_default_connect_timeout_applies() -> None:
    assert db_session._connect_args(_settings(), PG)["connect_timeout"] == 10


def test_a_sub_second_connect_timeout_is_still_a_timeout() -> None:
    assert db_session._connect_args(_settings(db_connect_timeout_seconds=0.4), PG)["connect_timeout"] == 1


def test_other_drivers_get_no_libpq_options() -> None:
    other = "postgresql+asyncpg://u:p@localhost:5432/db"
    assert db_session._connect_args(_settings(process_role="api"), other) == {}


def test_prepared_statements_off_still_travels_with_the_rest() -> None:
    args = db_session._connect_args(_settings(db_prepared_statements=False), PG)
    assert "prepare_threshold" in args and args["prepare_threshold"] is None
    assert args["application_name"] == "felix", "no role stamped: the bare name, not felix-"


def test_every_entrypoint_stamps_its_role() -> None:
    """The role is what `pg_stat_activity` will show; each console script must set it
    on the settings object before it builds an engine."""
    import inspect

    from felix_api import app as api_app
    from felix_cli import main as cli_main
    from felix_worker import main as worker_main
    from felix_worker import tasks as worker_tasks

    assert 'stamp_process_role("api")' in inspect.getsource(api_app)
    assert 'stamp_process_role("worker")' in inspect.getsource(worker_tasks)
    assert 'stamp_process_role("scheduler")' in inspect.getsource(worker_main)
    assert 'stamp_process_role("temporal-worker")' in inspect.getsource(worker_main)
    assert 'stamp_process_role("cli")' in inspect.getsource(cli_main._root), "the CLI stamps in its callback"
    assert "stamp_process_role" not in inspect.getsource(cli_main).split("def _root", 1)[0], "never at import"


def test_the_environment_names_the_process_first() -> None:
    settings = _settings(process_role="scheduler")
    settings.stamp_process_role("api")
    assert settings.application_name() == "felix-scheduler", "FELIX_PROCESS_ROLE wins over the entrypoint"
    with pytest.raises(ValueError):
        _settings(process_role="frontend")


def test_a_stamp_after_the_first_engine_is_loud(monkeypatch: pytest.MonkeyPatch, caplog: Any) -> None:
    """Engines are cached for the life of the process and read the name once; a late
    stamp names nothing, and that must not be silent."""
    import logging

    monkeypatch.setattr(db_session, "engines_exist", lambda: True)
    settings = _settings()
    with caplog.at_level(logging.WARNING, logger="felix.config"):
        settings.stamp_process_role("worker")
    assert any("after an engine was built" in r.getMessage() for r in caplog.records)


def test_migrations_get_the_same_driver_bounds() -> None:
    """`felix migrate` builds its own engine outside `get_engine`; it must not be the
    one connection with no timeout and no name. Structural: `migrations/env.py` is an
    Alembic script, not an importable module."""
    source = pathlib.Path("migrations/env.py").read_text(encoding="utf-8")
    assert "connect_args=_connect_args(get_settings(), url)" in source


def test_granian_gets_the_server_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Through `main()` — the console-script path — not by calling the helper."""
    import felix_api.main as api_main
    from felix.config import get_settings

    captured: dict[str, Any] = {}

    class _Server:
        def __init__(self, target: str, **kwargs: Any) -> None:
            captured.update(kwargs, target=target)

        def serve(self) -> None:
            captured["served"] = True

    import granian.server

    monkeypatch.setattr(granian.server, "Server", _Server)
    for key, value in {
        "FELIX_DATABASE_URL": "memory://granian",
        "FELIX_OBJECT_STORE": "memory",
        "FELIX_AUTH_MODE": "none",
        "FELIX_ALLOW_INSECURE": "true",
        "FELIX_HTTP_BACKLOG": "4096",
        "FELIX_HTTP_RUNTIME_THREADS": "2",
        "FELIX_GRACEFUL_SHUTDOWN_SECONDS": "90",
        "FELIX_RESPAWN_FAILED_WORKERS": "false",
        "FELIX_WORKERS": "3",
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    try:
        api_main.main()
    finally:
        get_settings.cache_clear()

    assert captured["served"] and captured["target"] == "felix_api.main:create_application"
    assert (captured["backlog"], captured["runtime_threads"], captured["workers_kill_timeout"]) == (
        4096,
        2,
        90,
    )
    assert captured["workers"] == 3 and captured["respawn_failed_workers"] is False


def test_task_results_expire() -> None:
    from felix_worker import tasks

    backend = tasks.broker.result_backend
    assert getattr(backend, "result_ex_time", None) == tasks._settings.task_result_ttl_seconds
    assert Settings(database_url="memory://t").task_result_ttl_seconds == 3600


def test_a_stamp_on_any_settings_reaches_the_instance_engines_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """`get_engine` reads the cached `get_settings()`; `create_app(settings=...)` stamps the
    instance it was handed. The stamp must land on both or the engine is unnamed."""
    from felix.config import get_settings

    monkeypatch.setenv("FELIX_DATABASE_URL", "memory://stamp")
    monkeypatch.setenv("FELIX_PROCESS_ROLE", "")
    get_settings.cache_clear()
    try:
        handed = _settings()
        handed.stamp_process_role("api")
        assert get_settings().application_name() == "felix-api"
    finally:
        get_settings.cache_clear()
