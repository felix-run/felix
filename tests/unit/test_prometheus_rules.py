"""The alert rules watch metrics that exist, from one file both deploy paths load.

A rule that names a metric the code no longer emits is a dashboard that never fires; a
rules file that Compose and Helm each keep a copy of drifts. Both are checked here.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.compose_yaml import load_compose
from tests.emitted_metrics import emitted_metric_names
from tests.helm import helm_or_skip, render

ROOT = Path(__file__).resolve().parents[2]
RULES = ROOT / "deploy/helm/felix/files/prometheus-rules.yml"
# Metrics that come from other exporters, or from Prometheus itself.
EXTERNAL = {"up", "pg_stat_activity_count", "pg_settings_max_connections"}


def _rules() -> list[dict]:
    from ruamel.yaml import YAML

    doc = YAML(typ="safe").load(RULES.read_text(encoding="utf-8"))
    return [rule for group in doc["groups"] for rule in group["rules"]]


PROMQL_WORDS = {
    "sum",
    "max",
    "min",
    "avg",
    "count",
    "by",
    "on",
    "without",
    "increase",
    "rate",
    "absent",
    "absent_over_time",
    "and",
    "or",
    "unless",
}


def _felix_metrics_in(expr: str) -> set[str]:
    """Every Felix metric an expression names, wherever it appears (a bare gauge too)."""
    return set(re.findall(r"\bfelix_[a-z0-9_]+", expr))


def _external_metrics_in(expr: str) -> set[str]:
    """Identifiers that are neither Felix metrics, PromQL words, functions nor labels."""
    stripped = re.sub(r"\{[^}]*\}|\[[^\]]*\]|\b(?:by|without|on|ignoring)\s*\([^)]*\)", "", expr)
    names = set(re.findall(r"\b([a-z_][a-z0-9_]*)\b(?!\s*\()", stripped))
    return {n for n in names if not n.startswith("felix_") and n not in PROMQL_WORDS}


def test_the_extractor_sees_a_bare_gauge() -> None:
    assert _felix_metrics_in("felix_active_fibers > 100") == {"felix_active_fibers"}
    assert _felix_metrics_in(
        'sum by (task) (increase(felix_worker_task_total{status="error"}[15m])) > 0'
    ) == {"felix_worker_task_total"}
    assert _external_metrics_in("sum(pg_stat_activity_count) / max(pg_settings_max_connections) > 0.8") == {
        "pg_stat_activity_count",
        "pg_settings_max_connections",
    }


def test_every_felix_metric_a_rule_names_is_emitted() -> None:
    emitted = emitted_metric_names()
    felix = {m for rule in _rules() for m in _felix_metrics_in(rule["expr"])}
    assert felix, "no Felix metric is referenced — the expression parser is wrong"
    unknown = {m for m in felix if re.sub(r"_(total|count|sum|bucket)$", "", m) not in emitted}
    assert not unknown, f"rules watch metrics the code does not emit: {sorted(unknown)}"
    external = {m for rule in _rules() for m in _external_metrics_in(rule["expr"])}
    assert external <= EXTERNAL, f"unexpected external metric: {sorted(external - EXTERNAL)}"


def test_counters_carry_the_wire_suffix() -> None:
    """prometheus_client exposes a Counter as `<name>_total`; a rule on the bare name
    matches nothing and never fires."""
    for rule in _rules():
        for metric in _felix_metrics_in(rule["expr"]):
            assert metric.endswith(("_total", "_count", "_sum", "_bucket")), (rule["alert"], metric)


def test_every_rule_has_a_severity_and_a_summary() -> None:
    for rule in _rules():
        assert rule["labels"]["severity"] in {"warning", "critical"}, rule["alert"]
        assert rule["annotations"]["summary"], rule["alert"]


def test_the_watch_this_metrics_are_alerted_on() -> None:
    """The catalog marks four rows **watch this**; each must have a rule, or the marking
    is advice nobody acts on."""
    doc = (ROOT / "docs/OBSERVABILITY.md").read_text(encoding="utf-8")
    watched = set(
        re.findall(r"^\|\s*`(felix_[a-z_]+)`\s*\|[^|]*\|\s*\*\*Watch this\.\*\*", doc, re.MULTILINE)
    )
    assert watched, "the catalog no longer marks anything watch-this — update this test"
    named = {re.sub(r"_total$", "", m) for rule in _rules() for m in _felix_metrics_in(rule["expr"])}
    assert watched <= named, f"watch-this metrics with no alert rule: {sorted(watched - named)}"


def test_compose_and_the_chart_load_the_same_file() -> None:
    compose = load_compose(ROOT / "deploy/docker/compose.observability.yml")
    mounts = compose["services"]["prometheus"]["volumes"]
    rel = RULES.relative_to(ROOT)
    assert any(str(m).startswith(f"./{rel}:") for m in mounts), mounts
    config = (ROOT / "deploy/docker/config/prometheus.yml").read_text(encoding="utf-8")
    target = next(str(m).split(":")[1] for m in mounts if str(m).startswith(f"./{rel}:"))
    assert target in config, "prometheus.yml rule_files does not name the mounted path"
    template = (ROOT / "deploy/helm/felix/templates/prometheusrule.yaml").read_text(encoding="utf-8")
    assert '.Files.Get "files/prometheus-rules.yml"' in template


def test_the_chart_renders_the_rules_when_enabled() -> None:
    helm_or_skip()
    rendered = render("prometheusRule.enabled=true")
    rules = [doc for (kind, _name), doc in rendered.items() if kind == "PrometheusRule"]
    assert len(rules) == 1, sorted(rendered)
    rule = rules[0]
    alerts = {r["alert"] for g in rule["spec"]["groups"] for r in g["rules"]}
    assert "FelixBufferDropped" in alerts
    assert "{{ $labels.task }}" in str(rule), "annotations must reach the operator un-templated by Helm"


def test_every_task_label_a_rule_names_is_an_instrumented_worker_task() -> None:
    """`absent_over_time(... task="fiber_scheduler")` fires forever if that task is renamed;
    the metric name would still exist, so the metric check above cannot see it."""
    import ast

    tasks = (ROOT / "apps/worker/src/felix_worker/tasks.py").read_text(encoding="utf-8")
    instrumented = {
        node.args[0].value
        for node in ast.walk(ast.parse(tasks))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_instrumented"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    }
    assert instrumented, "no _instrumented(...) tasks found — the scan is wrong"
    named = {m for rule in _rules() for m in re.findall(r'task="([a-z_]+)"', rule["expr"])}
    assert named, "no rule pins a task label — update this test if that is deliberate"
    assert named <= instrumented, f"rules name tasks the worker does not run: {sorted(named - instrumented)}"
