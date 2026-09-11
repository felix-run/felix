"""The Helm chart's topology, asserted on what `helm template` renders.

api, worker and scheduler were three containers in one Deployment. The Service, the HPA
and the PDB all selected that pod, so scaling the api scaled the scheduler with it and
every replica enqueued every cron sweep; the worker had no model key although it runs the
agent loop; the PDB's `minAvailable: 1` against one replica denied every node drain; and
the default image resolved to docker.io/library/felix, which does not exist.

Rendered rather than parsed: the properties below live in helper output and value
defaults, which a template file does not show. `helm` is on PATH in the CI helm job,
which sets FELIX_REQUIRE_HELM=1 so a missing binary fails there instead of skipping —
locally, without it, these skip and say so.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.helm import helm_or_skip, render

ROOT = Path(__file__).resolve().parents[2]

helm_or_skip()
_render = render


@pytest.fixture(scope="module")
def rendered() -> dict[tuple[str, str], dict[str, Any]]:
    return _render()


def _deployments(objects: dict) -> dict[str, dict]:
    """Deployments keyed by component label, so a change to how names are derived from
    the release does not fail every test here for nothing."""
    return {
        obj["metadata"]["labels"]["app.kubernetes.io/component"]: obj
        for (kind, _), obj in objects.items()
        if kind == "Deployment"
    }


def _only(objects: dict, kind: str) -> dict:
    found = [obj for (k, _), obj in objects.items() if k == kind]
    assert len(found) == 1, f"expected one {kind}, rendered {len(found)}"
    return found[0]


def _pod_labels(deployment: dict) -> dict:
    return deployment["spec"]["template"]["metadata"]["labels"]


def _container(deployment: dict, name: str) -> dict:
    return next(c for c in deployment["spec"]["template"]["spec"]["containers"] if c["name"] == name)


def _env_names(container: dict) -> set[str]:
    return {e["name"] for e in container.get("env", [])}


def test_each_process_is_its_own_deployment(rendered: dict) -> None:
    deployments = _deployments(rendered)
    assert set(deployments) == {"api", "worker", "scheduler"}
    for name, deployment in deployments.items():
        containers = deployment["spec"]["template"]["spec"]["containers"]
        assert len(containers) == 1, f"{name} runs more than one Felix process in a pod"


def test_the_scheduler_is_a_singleton(rendered: dict) -> None:
    """Every scheduler instance enqueues every cron sweep, so two double every flush."""
    scheduler = _deployments(rendered)["scheduler"]
    assert scheduler["spec"]["replicas"] == 1
    assert scheduler["spec"]["strategy"] == {"type": "Recreate"}, "an upgrade must not overlap two schedulers"


def test_the_hpa_targets_the_api_alone() -> None:
    objects = _render("api.autoscaling.enabled=true")
    hpa = _only(objects, "HorizontalPodAutoscaler")
    assert hpa["spec"]["scaleTargetRef"]["name"] == _deployments(objects)["api"]["metadata"]["name"]


def test_the_service_selects_only_api_pods(rendered: dict) -> None:
    """Worker and scheduler pods carry the same instance labels and no port."""
    selector = _only(rendered, "Service")["spec"]["selector"]
    deployments = _deployments(rendered)
    assert selector.items() <= _pod_labels(deployments["api"]).items(), "the Service must select the api pod"
    for name in ("worker", "scheduler"):
        assert not selector.items() <= _pod_labels(deployments[name]).items(), f"would route to {name}"


def test_the_default_pdb_does_not_block_a_drain(rendered: dict) -> None:
    """`minAvailable: 1` against the one-replica default denied every voluntary eviction."""
    pdb = _only(rendered, "PodDisruptionBudget")
    assert pdb["spec"].get("maxUnavailable") == 1
    assert "minAvailable" not in pdb["spec"]
    selector = pdb["spec"]["selector"]["matchLabels"]
    deployments = _deployments(rendered)
    assert selector.items() <= _pod_labels(deployments["api"]).items()
    for name in ("worker", "scheduler"):
        assert not selector.items() <= _pod_labels(deployments[name]).items(), f"would cover {name}"


def test_min_available_is_still_available_when_asked_for() -> None:
    objects = _render("api.podDisruptionBudget.maxUnavailable=null", "api.podDisruptionBudget.minAvailable=2")
    pdb = _only(objects, "PodDisruptionBudget")
    assert pdb["spec"].get("minAvailable") == 2 and "maxUnavailable" not in pdb["spec"]


def test_the_old_top_level_api_keys_fail_the_render_instead_of_being_ignored() -> None:
    """`resources` used to size the pod that ran every process; a stale override now
    applying to nothing would be the silent-default shape this repo keeps meeting."""
    for key in (
        "replicaCount=2",
        "resources.limits.cpu=2",
        "autoscaling.enabled=true",
        "podDisruptionBudget.enabled=false",
    ):
        with pytest.raises(subprocess.CalledProcessError) as exc:
            _render(key)
        assert f"moved to `api.{key.split('.')[0].split('=')[0]}`" in exc.value.stderr


def test_the_worker_carries_the_credentials_the_agent_loop_needs_and_no_more(rendered: dict) -> None:
    """Fiber resume and continuous eval run the agent loop on the worker, so it needs the
    model and object-store keys. It executes tools on model output, so it must not hold
    the token-signing key or the /internal shared secret — nothing on its path reads them."""
    deployments = _deployments(rendered)
    api = _env_names(_container(deployments["api"], "api"))
    worker = _env_names(_container(deployments["worker"], "worker"))
    assert {
        "FELIX_ANTHROPIC_API_KEY",
        "FELIX_OPENAI_API_KEY",
        "FELIX_S3_ACCESS_KEY",
        "FELIX_S3_SECRET_KEY",
    } <= worker
    assert api - worker == {
        "FELIX_JWKS_PUBLIC",
        "FELIX_JWKS_PRIVATE",
        "FELIX_CONSUMER_SHARED_SECRET",
        # Not a credential: the API's drain deadline, derived from its own grace period.
        "FELIX_GRACEFUL_SHUTDOWN_SECONDS",
    }, api - worker


def test_the_scheduler_only_gets_the_datastore_urls(rendered: dict) -> None:
    """It enqueues; it never runs a model. Least privilege is the point of the split."""
    scheduler = _env_names(_container(_deployments(rendered)["scheduler"], "scheduler"))
    assert scheduler == {"FELIX_DATA_DIR", "FELIX_DATABASE_URL", "FELIX_REDIS_URL"}, scheduler


def test_the_worker_liveness_probe_is_its_metrics_port(rendered: dict) -> None:
    """The worker has no HTTP server of its own; a wedged one was invisible and never
    restarted. The probe port and FELIX_METRICS_PORT must agree or the probe fails forever."""
    worker = _container(_deployments(rendered)["worker"], "worker")
    port = next(e["value"] for e in worker["env"] if e["name"] == "FELIX_METRICS_PORT")
    assert port not in ("", "0")
    named = {p["name"]: p["containerPort"] for p in worker["ports"]}
    probe_port = worker["livenessProbe"]["httpGet"]["port"]
    assert str(named.get(probe_port, probe_port)) == port


def test_a_zero_metrics_port_disables_the_probe_and_the_port_together() -> None:
    """Half of it left behind is a probe against a port nothing listens on."""
    worker = _container(_deployments(_render("worker.metricsPort=0"))["worker"], "worker")
    assert "livenessProbe" not in worker and "ports" not in worker
    assert next(e["value"] for e in worker["env"] if e["name"] == "FELIX_METRICS_PORT") == "0"


def test_the_api_probes_the_public_probe_paths(rendered: dict) -> None:
    """These two paths are what the API's auth allowlist exempts (test_operability pins
    that side); a chart pointing elsewhere gets 401 from kubelet's credential-less probe."""
    api = _container(_deployments(rendered)["api"], "api")
    assert api["readinessProbe"]["httpGet"]["path"] == "/ready"
    assert api["livenessProbe"]["httpGet"]["path"] == "/live"


def test_the_api_drains_before_it_stops(rendered: dict) -> None:
    api_deployment = _deployments(rendered)["api"]
    assert api_deployment["spec"]["template"]["spec"]["terminationGracePeriodSeconds"] >= 60
    api = _container(api_deployment, "api")
    assert api["lifecycle"]["preStop"]["exec"]["command"][0] == "sleep"


def test_the_default_image_is_registry_qualified(rendered: dict) -> None:
    """`repository: felix` resolved to docker.io/library/felix, which does not exist."""
    for name, deployment in _deployments(rendered).items():
        image = deployment["spec"]["template"]["spec"]["containers"][0]["image"]
        repository = image.rsplit(":", 1)[0]
        assert "/" in repository and "library/" not in repository, (name, image)


def test_the_worker_port_is_not_reachable_through_the_service(rendered: dict) -> None:
    """Its metrics are unauthenticated and carry tenant-supplied labels."""
    ports = _only(rendered, "Service")["spec"]["ports"]
    assert [p["targetPort"] for p in ports] == ["http"], ports


def test_the_chart_hands_the_api_its_drain_deadline(rendered: dict) -> None:
    """One number governs the drain: the pod's grace period minus the preStop sleep is
    what the API is told to give each worker, so `terminationGracePeriodSeconds: 120` is
    not decorative against an in-process default that cuts under it."""
    api = _container(_deployments(rendered)["api"], "api")
    env = {e["name"]: e.get("value") for e in api.get("env", [])}
    assert env["FELIX_GRACEFUL_SHUTDOWN_SECONDS"] == "115"
