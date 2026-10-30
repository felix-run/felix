"""The production telemetry overlay: one collector beside a GCE stack, exporting to Google Cloud.

The rendered-config half (no Felix service builds, every process waits on migrate) is
scripts/check-compose.sh's; these are the properties of the files themselves.
"""

from __future__ import annotations

from pathlib import Path

from tests.support.compose_yaml import load_compose as _load

ROOT = Path(__file__).resolve().parents[2]
OVERLAY = ROOT / "deploy/docker/compose.otel-gcp.yml"
COLLECTOR = ROOT / "deploy/docker/config/otel-collector-gcp.yaml"
LOCAL_OVERLAY = ROOT / "deploy/docker/compose.observability.yml"


def test_it_adds_only_the_collector_and_publishes_no_port() -> None:
    """Storage and dashboards live in Google Cloud; nothing here listens beyond the compose network.

    The worker's metrics port has no auth middleware, and the collector's receivers and health
    endpoint have none either.
    """
    services = _load(OVERLAY)["services"]
    assert set(services) == {"api", "worker", "otel-collector"}
    published = sorted(name for name, svc in services.items() if svc.get("ports"))
    assert not published, f"services publishing a host port: {published}"


def test_felix_services_keep_the_published_image() -> None:
    """compose.gcp.yml pulls the release; an overlay that re-adds `build:` compiles on the host."""
    for name in ("api", "worker"):
        assert "build" not in _load(OVERLAY)["services"][name], name


def test_the_collector_is_pinned_bounded_and_matches_the_local_stack() -> None:
    services = _load(OVERLAY)["services"]
    collector = services["otel-collector"]
    assert "@sha256:" in collector["image"]
    # One collector version across both overlays, so Dependabot bumps them together.
    assert collector["image"] == _load(LOCAL_OVERLAY)["services"]["otel-collector"]["image"]
    assert collector["mem_limit"], "an unbounded collector competes with the API for the VM"


def test_a_missing_scrape_token_fails_the_up() -> None:
    """A short-form bind of a missing file makes Docker create a directory there, and every API
    scrape then fails while the collector reports healthy."""
    collector = _load(OVERLAY)["services"]["otel-collector"]
    token = next(v for v in collector["volumes"] if isinstance(v, dict))
    assert token["source"] == "./deploy/docker/.metrics-token"
    assert token["bind"]["create_host_path"] is False
    assert token["read_only"] is True
    jobs = {j["job_name"]: j for j in _load(COLLECTOR)["receivers"]["prometheus"]["config"]["scrape_configs"]}
    assert jobs["felix-api"]["authorization"]["credentials_file"] == token["target"], (
        "the API scrape reads its token from somewhere the overlay does not mount it"
    )


def test_every_signal_leaves_the_host() -> None:
    pipelines = _load(COLLECTOR)["service"]["pipelines"]
    assert pipelines["traces"]["exporters"] == ["googlecloud"]
    assert pipelines["logs"]["exporters"] == ["googlecloud"]
    assert pipelines["metrics"]["exporters"] == ["googlemanagedprometheus"]
    for name, pipeline in pipelines.items():
        assert pipeline["processors"][0] == "memory_limiter", f"{name} is not memory-bounded first"


def test_the_overlay_pins_every_part_of_the_transport() -> None:
    """As in the local overlay: a hosted vendor's config left in .env must not reach this collector."""
    services = _load(OVERLAY)["services"]
    for name in ("api", "worker"):
        env = services[name]["environment"]
        assert env["FELIX_OTEL_ENABLED"] == "true"
        assert env["FELIX_OTEL_ENDPOINT"] == "http://otel-collector:4317"
        assert (env["FELIX_OTEL_PROTOCOL"], env["FELIX_OTEL_INSECURE"], env["FELIX_OTEL_HEADERS"]) == (
            "grpc",
            "true",
            "",
        )
        assert env["FELIX_OTEL_CAPTURE_CONTENT"] == "${FELIX_OTEL_CAPTURE_CONTENT:-false}"
    assert "ports" not in services["worker"] and services["worker"]["environment"]["FELIX_METRICS_PORT"]


def test_each_process_is_its_own_service_and_skips_created_series() -> None:
    """Both processes default to `felix`, which merges their spans in Cloud Trace; and the
    `_created` series double what Managed Prometheus bills for every counter."""
    services = _load(OVERLAY)["services"]
    names = {services[n]["environment"]["FELIX_OTEL_SERVICE_NAME"] for n in ("api", "worker")}
    assert len(names) == 2, names
    for name in ("api", "worker"):
        assert services[name]["environment"]["PROMETHEUS_DISABLE_CREATED_SERIES"] == "True"
