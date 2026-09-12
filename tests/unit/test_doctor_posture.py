"""`felix doctor` says whether the schema is current and whether the posture is safe.

"Database reachable" said nothing about migrations, and a production `.env` with an
empty `FELIX_ALLOWED_TENANTS` under a claim-mode JWT verifier, a plaintext OTLP exporter
off-host, or prompts captured into spans passed doctor green. Each is legal to configure
and quietly weakens the deployment; doctor is where an operator looks before a deploy,
so it says so there — and says what it skipped.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from felix.config import Settings, get_settings
from felix.db import migrations
from felix_cli.main import Finding, _capability_findings, _posture_findings

ROOT = Path(__file__).resolve().parents[2]
BASE = {
    "FELIX_DATABASE_URL": "memory://doctor",
    "FELIX_OBJECT_STORE": "memory",
    "FELIX_WAREHOUSE": "none",
    "FELIX_REDIS_URL": "redis://127.0.0.1:9/0",
}
CLAIM = "self:https://idp.example.com"
FIXED = "self:https://idp.example.com;tenant=fixed:acme"


def _settings(**kw: Any) -> Settings:
    return Settings(database_url="memory://posture", object_store="memory", **kw)


def _by_label(rows: list[Finding]) -> dict[str, Finding]:
    return {r.label: r for r in rows}


def _doctor(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **env: str) -> str:
    from felix_cli.main import app
    from typer.testing import CliRunner

    for key, value in {**BASE, "FELIX_DATA_DIR": str(tmp_path), **env}.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    try:
        return " ".join(CliRunner().invoke(app, ["doctor"]).output.split())
    finally:
        get_settings.cache_clear()


def test_the_script_head_is_the_newest_revision_file() -> None:
    newest = sorted(p.stem for p in (ROOT / "migrations/versions").glob("[0-9]*.py"))[-1]
    assert migrations.script_head() == newest


def test_the_alembic_config_does_not_depend_on_the_working_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`script_location = migrations` in alembic.ini is cwd-relative; `felix migrate` failed
    from anywhere but the repo root, and doctor would have too. And `prepend_sys_path = .`
    put that directory ahead of site-packages for every later import."""
    import sys

    monkeypatch.chdir(tmp_path)
    assert migrations.script_head() is not None
    assert "." not in sys.path and str(tmp_path) not in sys.path, "the working directory was put on sys.path"


@pytest.mark.asyncio
async def test_memory_has_no_schema_to_be_behind() -> None:
    state = await migrations.migration_state(Settings(database_url="memory://m"))
    assert state.at_head and state.current == state.head


def test_at_head_needs_a_head() -> None:
    assert not migrations.MigrationState(current=None, head=None).at_head
    assert not migrations.MigrationState(current="0001", head="0002").at_head
    assert migrations.MigrationState(current="0002", head="0002").at_head


def test_development_is_not_judged() -> None:
    rows = _posture_findings(
        _settings(auth_mode="none", allow_insecure=True, otel_enabled=True, otel_insecure=True)
    )
    assert all(r.passed for r in rows)
    assert not any("otel" in r.label or "allowed_tenants" in r.label for r in rows)


def test_a_claim_mode_jwt_without_an_allowlist_fails_and_fixed_does_not() -> None:
    claim = _by_label(
        _posture_findings(_settings(environment="production", auth_mode="jwt", jwt_verifiers=CLAIM))
    )
    row = claim["allowed_tenants pins the tenant claim"]
    assert not row.passed and "FELIX_ALLOWED_TENANTS=(empty)" in row.detail and "tenant=fixed" in row.remedy

    fixed = _posture_findings(_settings(environment="production", auth_mode="jwt", jwt_verifiers=FIXED))
    assert not any("allowed_tenants" in r.label for r in fixed), "a fixed-tenant verifier reads no claim"

    listed = _by_label(
        _posture_findings(
            _settings(environment="production", auth_mode="jwt", jwt_verifiers=CLAIM, allowed_tenants="acme")
        )
    )
    assert listed["allowed_tenants pins the tenant claim"].passed


@pytest.mark.parametrize(
    ("protocol", "endpoint", "insecure", "expect"),
    [
        ("grpc", "http://collector.example.com:4317", True, False),
        ("grpc", "http://collector.example.com:4317", False, True),
        ("http", "http://collector.example.com:4318", False, False),  # the flag is not read over http
        ("http", "https://ingest.vendor.example", True, True),  # nor does it make https plaintext
        ("grpc", "http://localhost:4317", True, True),  # a local collector is private
        ("grpc", "http://otel-collector:4317", True, False),  # an in-cluster service is not loopback
        ("grpc", "localhost:4317", True, True),  # the gRPC exporter accepts a schemeless endpoint
    ],
)
def test_otel_transport_is_judged_the_way_the_exporter_decides_it(
    protocol: str, endpoint: str, insecure: bool, expect: bool
) -> None:
    rows = _by_label(
        _posture_findings(
            _settings(
                environment="production",
                auth_mode="api_key",
                auth_api_keys="k",
                otel_enabled=True,
                otel_protocol=protocol,
                otel_endpoint=endpoint,
                otel_insecure=insecure,
            )
        )
    )
    assert rows["otel exporter is private or TLS"].passed is expect


def test_prompts_in_spans_are_a_finding_outside_development() -> None:
    rows = _by_label(
        _posture_findings(
            _settings(
                environment="staging",
                auth_mode="api_key",
                auth_api_keys="k",
                otel_enabled=True,
                otel_capture_content=True,
            )
        )
    )
    row = rows["otel spans exclude prompts"]
    assert not row.passed and "FELIX_OTEL_CAPTURE_CONTENT=true" in row.detail


def test_details_are_values_and_remedies_only_print_on_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A passing row used to carry a failure sentence as its detail, so a sound deployment
    read `ok ... — FELIX_ALLOWED_TENANTS empty — any tenant a JWT claims is accepted`."""
    out = _doctor(
        monkeypatch,
        tmp_path,
        FELIX_ENVIRONMENT="staging",
        FELIX_AUTH_MODE="jwt",
        FELIX_JWT_VERIFIERS=CLAIM,
        FELIX_ALLOWED_TENANTS="acme",
        FELIX_JWKS_PUBLIC="",
    )
    assert "ok allowed_tenants pins the tenant claim — FELIX_ALLOWED_TENANTS=acme" in out
    assert "any tenant a JWT claims" not in out


def test_allow_insecure_is_judged_only_under_auth_mode_none() -> None:
    """Under `none` it is the acknowledgement the boot guard demands; under real auth the
    flag has no effect outside development, so there is nothing to fail."""
    under_none = _by_label(
        _posture_findings(_settings(environment="production", auth_mode="none", allow_insecure=True))
    )
    assert under_none["allow_insecure (required for auth_mode=none outside development)"].passed
    with_auth = _posture_findings(
        _settings(environment="production", auth_mode="api_key", auth_api_keys="k", allow_insecure=True)
    )
    assert not any("allow_insecure" in r.label for r in with_auth)


def test_doctor_prints_the_findings_the_skip_and_the_schema_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Wiring: the rows reach doctor's output, development says it skipped, and a schema
    behind the code is a FAIL."""

    async def behind(settings: Any) -> migrations.MigrationState:
        return migrations.MigrationState(current="0009_old", head="0011_usage_cost")

    monkeypatch.setattr(migrations, "migration_state", behind)
    out = _doctor(
        monkeypatch,
        tmp_path,
        FELIX_ENVIRONMENT="staging",
        FELIX_AUTH_MODE="jwt",
        FELIX_JWT_VERIFIERS=CLAIM,
        FELIX_JWKS_PUBLIC="",
    )
    assert "FAIL allowed_tenants pins the tenant claim" in out
    assert "FAIL migrations at head" in out and "run `felix migrate head`" in out
    assert "posture checks skipped" not in out

    dev = _doctor(
        monkeypatch,
        tmp_path,
        FELIX_ENVIRONMENT="development",
        FELIX_AUTH_MODE="none",
        FELIX_ALLOW_INSECURE="true",
    )
    assert "production posture checks skipped — FELIX_ENVIRONMENT=development" in dev


@pytest.mark.parametrize("environment", ["development", "production"])
@pytest.mark.parametrize(("installed", "expect"), [(False, False), (True, True)])
def test_doctor_reports_whether_the_otel_exporter_is_installed(
    monkeypatch: pytest.MonkeyPatch, environment: str, installed: bool, expect: bool
) -> None:
    """`FELIX_OTEL_ENABLED=true` without the extra exports nothing and logs one warning.

    Both environments, and that is the point rather than thoroughness: `_posture_findings`
    returns early under `development`, which is what `deploy/docker/compose.yml` defaults
    to — so a row placed there would be skipped for exactly the operator this exists for
    (`make up`, forget `FELIX_DOCKER_EXTRAS=otel`, run doctor, see nothing).

    Both arms of `installed`, because a row hardcoded to `False` would be just as green
    here and would show a permanent FAIL to everyone who installed the extra correctly.
    """
    # `_otel_findings` imports it from the harness at call time, which is the name the
    # running code resolves — patching a copy on felix_cli would pass while doctor did not.
    monkeypatch.setattr("felix.observability.tracing.trace_exporter_available", lambda _s: installed)
    rows = _by_label(
        _capability_findings(
            _settings(
                environment=environment,
                auth_mode="api_key",
                auth_api_keys="k",
                otel_enabled=True,
                otel_protocol="http",
            )
        )
    )
    assert "otel exporter is installed" in rows, (
        f"doctor is silent about a missing exporter under FELIX_ENVIRONMENT={environment}"
    )
    row = rows["otel exporter is installed"]
    assert row.passed is expect
    assert "FELIX_OTEL_PROTOCOL=http" in row.detail
    assert "FELIX_DOCKER_EXTRAS=otel" in row.remedy


@pytest.mark.parametrize(
    ("protocol", "present", "expect"),
    [
        ("grpc", "opentelemetry.exporter.otlp.proto.grpc.trace_exporter", True),
        ("grpc", "opentelemetry.exporter.otlp.proto.http.trace_exporter", False),
        ("http", "opentelemetry.exporter.otlp.proto.http.trace_exporter", True),
        ("http", "opentelemetry.exporter.otlp.proto.grpc.trace_exporter", False),
    ],
)
def test_the_exporter_probe_answers_for_the_configured_protocol(
    monkeypatch: pytest.MonkeyPatch, protocol: str, present: str, expect: bool
) -> None:
    """ "One of the two is installed" would call a broken configuration healthy.

    `_build_exporter` imports exactly one module, chosen by `otel_transport`. An
    environment carrying only the http exporter under `FELIX_OTEL_PROTOCOL=grpc` exports
    nothing, which is the failure the row exists to preempt — so the probe has to ask the
    same question the exporter does.
    """
    import importlib.util

    from felix.observability.tracing import trace_exporter_available

    # Every answer is stubbed, including the SDK's, so this measures the branch rather
    # than the venv — it must mean the same thing on a lean install, where the real
    # `find_spec` would answer None for all of them and hide the selection entirely.
    def only(name: str) -> object | None:
        return object() if name in (present, "opentelemetry.sdk.trace") else None

    monkeypatch.setattr(importlib.util, "find_spec", only)
    assert trace_exporter_available(_settings(otel_enabled=True, otel_protocol=protocol)) is expect


def test_the_exporter_probe_survives_a_missing_parent_package() -> None:
    """`find_spec` raises rather than answering None when a parent is absent.

    A lean install has no `opentelemetry` package at all, so the probe's own import
    machinery is the thing most likely to be missing — an unhandled raise there would turn
    `felix doctor` into a traceback on precisely the deployment it is meant to diagnose.
    """
    import importlib.util

    from felix.observability.tracing import trace_exporter_available

    def raiser(name: str) -> object:
        raise ModuleNotFoundError(f"No module named {name!r}")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(importlib.util, "find_spec", raiser)
        assert trace_exporter_available(_settings(otel_enabled=True)) is False


def test_the_otel_rows_are_absent_when_export_is_off() -> None:
    """Every otel row is conditional on export being on; a disabled exporter is not a fault."""
    settings = _settings(environment="production", auth_mode="api_key", auth_api_keys="k")
    assert _capability_findings(settings) == []
    assert not [r for r in _posture_findings(settings) if "otel" in r.label]
