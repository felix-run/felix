"""Cross-process delivery needs Redis, and its absence must be loud.

`waiters.wait` is a Redis BLPOP with an in-process future as the fallback. The fallback is
correct for one process and silently wrong for two: an approval decided on the API lands in
the API's memory while the worker's fiber waits, times out, and denies. `validate_runtime`
did not care and `felix doctor` did not say what the check was for. (The helper's own
warn-once behaviour is asserted in `test_redis_conn.py`, with the rest of the helper.)
"""

from __future__ import annotations

from pathlib import Path

import pytest
from felix.config import Settings, get_settings

DEV = {"auth_mode": "none", "allow_insecure": True, "host": "127.0.0.1", "database_url": "memory://redis"}


def test_an_empty_redis_url_is_refused_outside_development() -> None:
    with pytest.raises(RuntimeError, match="FELIX_REDIS_URL is required"):
        Settings(
            environment="production", auth_mode="api_key", auth_api_keys="k", redis_url="  "
        ).validate_runtime()


def test_development_may_run_without_redis() -> None:
    Settings(environment="development", redis_url="", **DEV).validate_runtime()  # type: ignore[arg-type]


def test_doctor_fails_on_an_empty_redis_url_outside_development(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from felix_cli.main import app
    from typer.testing import CliRunner

    for key, value in {
        "FELIX_DATABASE_URL": "memory://doctor",
        "FELIX_OBJECT_STORE": "memory",
        "FELIX_AUTH_MODE": "api_key",
        "FELIX_AUTH_API_KEYS": "k",
        "FELIX_ENVIRONMENT": "staging",
        "FELIX_REDIS_URL": "",
        "FELIX_DATA_DIR": str(tmp_path),
        "FELIX_WAREHOUSE": "none",
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    try:
        result = CliRunner().invoke(app, ["doctor"])
    finally:
        get_settings.cache_clear()

    out = " ".join(result.output.split())
    assert "FAIL redis (cross-process approvals" in out, out
    assert "FELIX_REDIS_URL empty" in out
