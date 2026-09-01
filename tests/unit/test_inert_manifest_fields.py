"""Manifest fields that were declared, documented, and enforced nowhere.

Each of these read as a control to anyone writing a manifest. A field that looks like an
access control and is not one is worse than a missing feature, so each is now either
enforced or its default corrected to match the behaviour it actually had.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from felix.config import Settings
from felix.context import AuthContext
from felix.manifests.governance import GovernanceError, assert_outbound_providers_allowed
from felix.manifests.inbound_auth import InboundAuthError, enforce_inbound_auth
from felix.manifests.loader import parse_manifest

ROOT = Path(__file__).resolve().parents[2]


def _manifest(spec: dict) -> object:
    return parse_manifest(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": "m"},
            "spec": spec,
        }
    )


def _settings(**kw: object) -> Settings:
    base: dict[str, object] = {
        "database_url": "memory://inert",
        "object_store": "memory",
        "allow_insecure": True,
        "auth_mode": "none",
    }
    base.update(kw)
    return Settings(**base)  # type: ignore[arg-type]


# --- auth.inbound.schemes -------------------------------------------------------


def _auth(scheme: str) -> AuthContext:
    return AuthContext(principal_sub="u", tenant_id="t", anonymous=False, scheme=scheme)


def test_scheme_not_in_allowlist_is_denied() -> None:
    """A manifest naming [jwt] previously accepted an api_key principal."""
    m = _manifest({"auth": {"inbound": {"schemes": ["jwt"], "allow_anonymous": False}}})
    with pytest.raises(InboundAuthError) as e:
        enforce_inbound_auth(m, _auth("api_key"))
    assert e.value.status_code == 403


def test_allowed_scheme_passes() -> None:
    m = _manifest({"auth": {"inbound": {"schemes": ["api_key"], "allow_anonymous": False}}})
    enforce_inbound_auth(m, _auth("api_key"))


def test_jwt_is_an_umbrella_for_verifier_schemes() -> None:
    """`schemes: [jwt]` should not force naming every IdP the deploy might use."""
    m = _manifest({"auth": {"inbound": {"schemes": ["jwt"], "allow_anonymous": False}}})
    for s in ("access", "cognito", "self"):
        enforce_inbound_auth(m, _auth(s))


def test_empty_schemes_allows_anything() -> None:
    m = _manifest({"auth": {"inbound": {"schemes": [], "allow_anonymous": False}}})
    enforce_inbound_auth(m, _auth("api_key"))


def test_schemes_do_not_override_anonymous_rule() -> None:
    """Anonymous is governed by allow_anonymous, not by the scheme list."""
    m = _manifest({"auth": {"inbound": {"schemes": ["jwt"], "allow_anonymous": True}}})
    enforce_inbound_auth(m, AuthContext(anonymous=True))


# --- auth.outbound.providers ----------------------------------------------------


def test_disallowed_provider_fails_the_build() -> None:
    """A manifest naming [anthropic] could still route to OpenAI."""
    m = _manifest({"auth": {"outbound": {"providers": ["anthropic"]}}, "model": {"id": "gpt-4.1"}})
    with pytest.raises(GovernanceError, match=re.escape("gpt-4.1")):
        assert_outbound_providers_allowed(m, _settings())


def test_allowed_provider_passes() -> None:
    m = _manifest({"auth": {"outbound": {"providers": ["anthropic"]}}, "model": {"id": "claude-sonnet-4"}})
    assert_outbound_providers_allowed(m, _settings())


def test_fallback_models_are_checked_too() -> None:
    m = _manifest(
        {
            "auth": {"outbound": {"providers": ["anthropic"]}},
            "model": {"id": "claude-sonnet-4", "fallbacks": ["gpt-4.1"]},
        }
    )
    with pytest.raises(GovernanceError, match=re.escape("gpt-4.1")):
        assert_outbound_providers_allowed(m, _settings())


def test_empty_providers_allows_anything() -> None:
    assert_outbound_providers_allowed(_manifest({"model": {"id": "gpt-4.1"}}), _settings())


# --- observability.metrics ------------------------------------------------------


def test_metric_allowlist_drops_unlisted_counters() -> None:
    import asyncio

    from felix.context import RequestContext, async_run_with_context
    from felix.observability.metrics import _metric_allowed

    async def _run() -> tuple[bool, bool]:
        ctx = RequestContext(settings=_settings(), auth=AuthContext())
        ctx.metric_names = frozenset({"felix_allowed"})
        async with async_run_with_context(ctx):
            return _metric_allowed("felix_allowed"), _metric_allowed("felix_other")

    allowed, other = asyncio.run(_run())
    assert allowed is True
    assert other is False


def test_no_allowlist_records_everything() -> None:
    from felix.observability.metrics import _metric_allowed

    assert _metric_allowed("anything") is True


# --- a2a.publish / capabilities / skills ----------------------------------------


def test_publish_defaults_true_to_preserve_existing_behaviour() -> None:
    """The field was never read, so every agent was advertised. Honouring the old
    `False` default would have 404'd the card for every existing manifest."""
    from felix.a2a.card import is_published

    assert is_published(_manifest({})) is True


def test_publish_false_is_honoured() -> None:
    from felix.a2a.card import is_published

    assert is_published(_manifest({"a2a": {"publish": False}})) is False


def test_declared_capabilities_reach_the_card() -> None:
    from felix.a2a.card import build_agent_card

    m = _manifest({"a2a": {"capabilities": [{"id": "summarize", "description": "d"}]}})
    card = build_agent_card(m)
    declared = card["capabilities"]["declared"]
    assert declared == [{"id": "summarize", "description": "d"}]
    # transport capabilities are still advertised
    assert card["capabilities"]["streaming"] is True


def test_manifest_skills_reach_the_card() -> None:
    """The card hardcoded `"skills": []` regardless of spec.skills."""
    from felix.a2a.card import build_agent_card

    m = _manifest({"skills": [{"name": "calculator-help", "description": "math"}]})
    card = build_agent_card(m)
    assert card["skills"] == [{"id": "calculator-help", "name": "calculator-help", "description": "math"}]


# --- anomaly --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_anomaly_thresholds_come_from_the_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    from felix.jobs import anomaly

    class _Spec:
        enabled = True
        min_volume = 1
        baseline_factor = 1.5

    async def _spec_for(settings, tenant_id, manifest_id):
        return _Spec()

    monkeypatch.setattr(anomaly, "_spec_for", _spec_for)
    ts = anomaly.now_ms()
    events = [{"manifest_id": "m", "ts": ts - 1000} for _ in range(3)]
    events += [{"manifest_id": "m", "ts": ts - anomaly.WINDOW_MS - 1000} for _ in range(24)]

    async def _list_events(*a, **k):
        return events, None

    monkeypatch.setattr(anomaly.audit_store, "list_events", _list_events)
    found = await anomaly.run_anomaly_scan(_settings())
    assert found and found[0]["min_volume"] == 1
    assert found[0]["threshold_factor"] == 1.5


@pytest.mark.asyncio
async def test_anomaly_disabled_manifest_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """`enabled: false` did not disable anything."""
    from felix.jobs import anomaly

    class _Off:
        enabled = False
        min_volume = 1
        baseline_factor = 1.0

    async def _spec_for(settings, tenant_id, manifest_id):
        return _Off()

    monkeypatch.setattr(anomaly, "_spec_for", _spec_for)
    ts = anomaly.now_ms()
    events = [{"manifest_id": "m", "ts": ts - 1000} for _ in range(50)]
    events += [{"manifest_id": "m", "ts": ts - anomaly.WINDOW_MS - 1000} for _ in range(24)]

    async def _list_events(*a, **k):
        return events, None

    monkeypatch.setattr(anomaly.audit_store, "list_events", _list_events)
    assert await anomaly.run_anomaly_scan(_settings()) == []


# --- the class, not just the instances ------------------------------------------

# Declared in `schema.py` and read nowhere. Each is a promise the schema makes and the
# harness does not keep: `felix validate-manifest` accepts it, the editor completes it, and
# it changes nothing. Left in place only because deciding wire-or-remove is a separate call
# per field — but the set may not grow, and shrinking it is the point.
KNOWN_INERT_FIELDS = {
    "after_facts",  # MemoryConsolidate
    "consolidate",  # MemorySpec
    "default_window_chars",  # ArtifactsSpec
    "max_window_chars",  # ArtifactsSpec
    "executor_model",  # PlanExecuteSpec
    "max_replans",  # PlanExecuteSpec
    "planner_few_shots",  # PlanExecuteSpec
    "planner_model",  # PlanExecuteSpec
    "replan_on_failure",  # PlanExecuteSpec
    "min_rate",  # AnomalySpec
    "precount",  # Limits
    "retention_days",  # GovernanceSpec
}


def test_the_set_of_fields_that_do_nothing_does_not_grow() -> None:
    """Every field the manifest schema declares should be read by something.

    A field that validates and does nothing is worse than a missing feature: it reads as a
    control, `validate-manifest` blesses it, and the only way to discover the truth is to
    grep the harness. This repo has shipped that shape repeatedly — `spec.a2a.capabilities`,
    `spec.anomaly`, `spec.auth.inbound.schemes`, `spec.observability.metrics`, all fixed
    above — so the useful guard is on the class rather than on each instance.

    A ratchet, deliberately: adding an unread field fails, and *fixing* one also fails until
    the name is removed from the set. Both edits should be conscious.

    Name-based, so it cannot see a field whose name is read on a different class —
    `SandboxRef.args_schema` was inert while `QueueRef.args_schema` was live. Those were
    removed by inspection; this catches the rest.
    """
    schema_path = ROOT / "packages/harness/src/felix/manifests/schema.py"
    tree = ast.parse(schema_path.read_text(encoding="utf-8"))
    declared: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    declared.add(item.target.id)

    corpus: list[str] = []
    for base in ("packages", "apps"):
        for path in (ROOT / base).rglob("*.py"):
            if path == schema_path or "test" in path.parts:
                continue
            corpus.append(path.read_text(encoding="utf-8", errors="ignore"))
    blob = "\n".join(corpus)

    unread = {f for f in declared if not re.search(rf"\b{re.escape(f)}\b", blob)}

    new = sorted(unread - KNOWN_INERT_FIELDS)
    assert not new, (
        "new manifest field with no reader in packages/ or apps/ — wire it or drop it "
        f"rather than shipping a field that validates and does nothing: {new}"
    )
    fixed = sorted(KNOWN_INERT_FIELDS - unread)
    assert not fixed, (
        "these fields now have readers; remove them from KNOWN_INERT_FIELDS so the ratchet "
        f"keeps its meaning: {fixed}"
    )
