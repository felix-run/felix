"""Properties of the observability overlay that are easy to break and invisible when broken.

Each of these was a real defect during development, found by running the stack rather than
by reading it — which is the point. A compose file parses fine while doing nothing.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
OVERLAY = ROOT / "deploy/docker/compose.observability.yml"
COLLECTOR = ROOT / "deploy/docker/config/otel-collector.yaml"
PROMETHEUS = ROOT / "deploy/docker/config/prometheus.yml"


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def test_the_overlay_builds_the_image_with_the_otel_extra() -> None:
    """The lean image has no otel extra, so OTLP export silently does nothing without this.

    The symptom is a single warning line at startup and an empty Jaeger — the stack looks
    healthy in every other respect.
    """
    services = _load(OVERLAY)["services"]
    for name in ("api", "worker"):
        extras = services[name]["build"]["args"]["FELIX_EXTRAS"]
        assert "otel" in extras, f"{name} would run without felix-harness[otel] and export nothing"


def test_the_worker_metrics_port_is_never_published() -> None:
    """It has no auth middleware and carries tenant-supplied manifest ids in its labels."""
    services = _load(OVERLAY)["services"]
    worker = services["worker"]
    assert worker["environment"]["FELIX_METRICS_PORT"], "the worker exposes no metrics"
    assert "ports" not in worker, (
        "the worker's metrics port is unauthenticated; publishing it exposes every "
        "tenant's manifest and tool names"
    )


def test_every_image_is_pinned_by_digest() -> None:
    """Dependabot watches /deploy/docker, and a floating tag is not a reproducible stack."""
    unpinned = [
        f"{name}: {svc['image']}"
        for name, svc in _load(OVERLAY)["services"].items()
        if "image" in svc and "@sha256:" not in svc["image"]
    ]
    assert not unpinned, f"images pinned by tag only: {unpinned}"


def test_every_service_has_a_memory_limit() -> None:
    """This overlay roughly triples the stack's footprint; unbounded services would evict Felix."""
    services = _load(OVERLAY)["services"]
    own = {n: s for n, s in services.items() if "image" in s}
    missing = sorted(n for n, s in own.items() if "mem_limit" not in s)
    assert not missing, f"services with no mem_limit: {missing}"


def test_the_collector_defines_no_exporter_it_cannot_start_with() -> None:
    """The collector validates every *defined* exporter, not only the ones a pipeline uses.

    An exporter whose endpoint comes from an unset variable therefore fails startup even
    when nothing references it — which is how a Memoturn exporter left in this file
    crash-looped the whole overlay for anyone without a Memoturn instance.
    """
    config = _load(COLLECTOR)
    for name, exporter in (config.get("exporters") or {}).items():
        endpoint = str((exporter or {}).get("endpoint", ""))
        if "${env:" in endpoint:
            assert ":-" in endpoint, (
                f"exporter {name!r} takes its endpoint from an unset-able variable with no "
                "default; the collector refuses to start when it is missing"
            )


def test_the_collector_does_not_tail_host_container_logs() -> None:
    """Rejected deliberately: it needs root, sees every container, and fails silently."""
    config = _load(COLLECTOR)
    assert "filelog" not in (config.get("receivers") or {}), (
        "filelog needs the collector to run as root to read root:root 0640 files and "
        "opens zero files without saying so; logs arrive over OTLP instead"
    )
    overlay = _load(OVERLAY)["services"]["otel-collector"]
    mounts = [v for v in overlay.get("volumes", []) if "/var/lib/docker" in v]
    assert not mounts, f"collector mounts host container logs: {mounts}"


def test_optional_overlay_targets_are_file_discovered() -> None:
    """A static target for a service that is usually down trains operators to ignore red."""
    jobs = {j["job_name"]: j for j in _load(PROMETHEUS)["scrape_configs"]}
    assert "overlays" in jobs, "no file_sd job for optional overlays"
    assert jobs["overlays"].get("file_sd_configs"), "the overlays job discovers nothing"
    for name, job in jobs.items():
        targets = [t for sc in job.get("static_configs", []) for t in sc.get("targets", [])]
        assert "temporal" not in " ".join(targets), (
            f"job {name!r} statically targets temporal, which is not in this overlay"
        )


def test_the_api_scrape_carries_a_credential() -> None:
    """/metrics is authenticated on purpose; an anonymous scrape would 401 forever."""
    jobs = {j["job_name"]: j for j in _load(PROMETHEUS)["scrape_configs"]}
    auth = jobs["felix-api"].get("authorization") or {}
    assert auth.get("credentials_file"), (
        "the felix-api scrape has no credential; /metrics is not public because its labels "
        "carry tenant-supplied manifest ids and remote MCP tool names"
    )
