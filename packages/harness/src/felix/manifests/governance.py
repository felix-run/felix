"""Compile-time governance validation and transparency helpers."""

from __future__ import annotations

from typing import Any

from felix.manifests.schema import Manifest, any_limit
from felix.manifests.secret_refs import PlaintextSecretError, assert_no_plaintext_secrets


class GovernanceError(ValueError):
    """Manifest fails opt-in framework requirements."""


def transparency_notice_text(manifest_name: str) -> str:
    return (
        f"You are talking to an AI agent ({manifest_name}). "
        "This interaction is assisted by automated systems."
    )


def apply_transparency_notice(system_prompt: str, manifest_name: str) -> str:
    notice = transparency_notice_text(manifest_name)
    if notice in (system_prompt or ""):
        return system_prompt
    if system_prompt:
        return f"{notice}\n\n{system_prompt}"
    return notice


def _effective_forbid_plaintext(manifest: Manifest, settings: Any | None) -> bool:
    if manifest.spec.governance.forbid_plaintext_secrets:
        return True
    return bool(settings is not None and getattr(settings, "environment", "") == "production")


def _has_access_control(manifest: Manifest) -> bool:
    inbound = manifest.spec.auth.inbound
    return bool(inbound.schemes) or bool(inbound.required_scopes)


def _has_boundary_control(manifest: Manifest) -> bool:
    return bool(manifest.spec.policies) or bool(manifest.spec.approvals) or any_limit(manifest.spec.limits)


def _has_data_governance(manifest: Manifest) -> bool:
    if manifest.spec.content_screening.enabled:
        return True
    g = manifest.spec.guardrails
    if g.providers and ("input" in g.targets or not g.targets):
        return True
    return bool(g.judges)


def assert_stdio_allowed(manifest: Manifest, settings: Any | None = None) -> None:
    """Reject stdio MCP servers whose command the operator has not allowlisted.

    Applied on manifest *write* as well as at compile, so a hostile manifest cannot be
    stored and then executed by the next request that resolves it.
    """
    from felix.security.stdio_policy import StdioNotAllowedError, assert_stdio_command_allowed

    errors: list[str] = []
    for ref in manifest.spec.mcp or []:
        if ref.transport != "stdio":
            continue
        try:
            assert_stdio_command_allowed(ref.command, settings)
        except StdioNotAllowedError as e:
            errors.append(f"mcp_servers[{ref.name}]: {e}")
    if errors:
        raise GovernanceError("; ".join(errors))


def assert_outbound_providers_allowed(manifest: Manifest, settings: Any | None = None) -> None:
    """Enforce ``spec.auth.outbound.providers``.

    The field was declared and never read, so a manifest naming ``[anthropic]`` could
    still route to OpenAI or a local Ollama. Checked at compile so a violation fails the
    build with a clear message rather than at the first model call.
    """
    allowed = {
        str(p).strip().lower() for p in (manifest.spec.auth.outbound.providers or []) if str(p).strip()
    }
    if not allowed:
        return

    from felix.config import get_settings
    from felix.patterns.model import parse_model_routes

    settings = settings or get_settings()
    routes = parse_model_routes(settings)
    spec = manifest.spec.model
    wanted = [getattr(spec, "id", None) or settings.default_model_id]
    wanted += list(getattr(spec, "fallbacks", None) or [])

    errors: list[str] = []
    for logical_id in wanted:
        route = routes.get(str(logical_id))
        if route is None:
            continue  # unknown ids are reported by the model layer, not here
        if route.provider.lower() not in allowed:
            errors.append(
                f"model {logical_id!r} routes to provider {route.provider!r}, "
                f"not in auth.outbound.providers ({', '.join(sorted(allowed))})"
            )
    if errors:
        raise GovernanceError("; ".join(errors))


def assert_cost_limit_is_measurable(manifest: Manifest, settings: Any | None = None) -> None:
    """Refuse a declared `limits.max_cost_usd` on a model Felix cannot price.

    A spend cap is a governance control, and an uncountable one fails *open*: the run
    proceeds with `cost_usd` stuck at zero and the cap never trips. Previously it failed
    open in a worse way — the catalog's default rates are Claude Sonnet's, so an
    unrecognised model was metered at $3/$15 per Mtok, which is enforcement against a
    number nobody chose.

    Only an *explicitly declared* cap is refused. `effective_limits` fills an unset
    `max_cost_usd` from `ABSOLUTE_LIMITS`, and refusing on that would break every local
    Ollama deployment for a ceiling the author never asked for. Declaring one is a
    statement that spend matters here, and that is the statement Felix must not fake.

    The escape hatch is `spec.model.price`, which is the documented way to supply rates
    per deployment.
    """
    limits = getattr(manifest.spec, "limits", None)
    declared = getattr(limits, "max_cost_usd", None) if limits is not None else None
    if declared is None:
        return
    spec = manifest.spec.model
    if getattr(spec, "price", None):
        return  # rates supplied by the manifest itself

    from felix_ai.providers import builtin_provider_specs

    from felix.config import get_settings
    from felix.model_catalog import is_priced
    from felix.patterns.model import parse_model_routes

    settings = settings or get_settings()
    routes = parse_model_routes(settings)
    wanted = [getattr(spec, "id", None) or settings.default_model_id]
    wanted += list(getattr(spec, "fallbacks", None) or [])

    free = {s.name for s in builtin_provider_specs() if not s.bills_per_token}

    unpriced: list[str] = []
    for logical_id in wanted:
        route = routes.get(str(logical_id))
        if route is None:
            continue  # unknown ids are reported by the model layer, not here
        if route.provider in free:
            continue  # spend on a local runtime is zero, so any cap holds
        if not is_priced(route.model):
            unpriced.append(f"{logical_id} -> {route.model}")
    if unpriced:
        raise GovernanceError(
            f"limits.max_cost_usd is declared but Felix has no rates for {', '.join(unpriced)}, "
            "so the cap could not be enforced. Supply rates with spec.model.price, or drop "
            "the limit."
        )


def validate_for_write(manifest: Manifest, settings: Any | None = None) -> None:
    """Every check a manifest must pass before it is *stored*, in one place.

    The route and the CLI used to keep their own chains of these and drifted: the CLI
    said `ok` in development to a manifest the route refused. A stored manifest that
    fails any of these is worse than a refused one — it compiles into a spawned command,
    a 500 per request, or a credential served to `manifests:read`.
    """
    from felix.manifests.secret_refs import PlaintextSecretError, assert_no_plaintext_secrets
    from felix.session.store import validate_checkpointer_config
    from felix.tools.sandboxes import SandboxImageNotAllowed, assert_sandbox_images_allowed

    assert_stdio_allowed(manifest, settings)
    # `memory.checkpointer` is an open string resolved against a registry, so pydantic no
    # longer catches a typo — and stored, it would raise inside every build.
    spec = manifest.spec
    try:
        validate_checkpointer_config(
            spec.memory.checkpointer,
            session_strategy=spec.session.strategy,
            compact_after_turn=spec.session.compact_after_turn,
            memory_capture=spec.memory.capture.enabled,
            memory_recall_tools=spec.memory.recall.tools,
        )
    except ValueError as exc:
        raise GovernanceError(str(exc)) from exc
    try:
        assert_no_plaintext_secrets(manifest, strict=_effective_forbid_plaintext(manifest, settings))
    except PlaintextSecretError as exc:
        raise GovernanceError(str(exc)) from exc
    try:
        assert_sandbox_images_allowed(manifest.spec.sandboxes, settings)
    except SandboxImageNotAllowed as exc:
        raise GovernanceError(str(exc)) from exc


def validate_governance(manifest: Manifest, settings: Any | None = None) -> None:
    """Fail closed when ``spec.governance.frameworks`` require missing controls.

    Empty ``frameworks`` is a no-op (local DX manifests stay valid).
    """
    gov = manifest.spec.governance
    frameworks = list(gov.frameworks or [])
    errors: list[str] = []

    # Frameworks force the confidentiality / drift flags on.
    if frameworks:
        if not gov.forbid_plaintext_secrets:
            errors.append("governance.forbid_plaintext_secrets must be true when frameworks are set")
        if not gov.pin_compile:
            errors.append("governance.pin_compile must be true when frameworks are set")

    forbid = _effective_forbid_plaintext(manifest, settings)
    if forbid or "soc2" in frameworks or "eu_ai_act" in frameworks:
        try:
            assert_no_plaintext_secrets(manifest)
        except PlaintextSecretError as e:
            errors.append(str(e))

    env = getattr(settings, "environment", "development") if settings is not None else "development"

    if "soc2" in frameworks:
        if manifest.spec.auth.inbound.allow_anonymous and env != "development":
            errors.append("soc2: auth.inbound.allow_anonymous must be false outside development")
        if not manifest.spec.observability.trace:
            errors.append("soc2: observability.trace must be true")
        if not manifest.spec.anomaly.enabled:
            errors.append("soc2: anomaly.enabled must be true")
        if not _has_boundary_control(manifest):
            errors.append("soc2: require non-empty policies, approvals, or limits")
        if not _has_access_control(manifest):
            errors.append("soc2: auth.inbound.schemes or required_scopes must be set")

    if "eu_ai_act" in frameworks:
        if not gov.transparency_notice:
            errors.append("eu_ai_act: governance.transparency_notice must be true")
        if gov.risk_tier == "high":
            if not manifest.spec.approvals:
                errors.append("eu_ai_act high risk: approvals must be non-empty")
            if any(a.allow_unattended for a in manifest.spec.approvals):
                errors.append("eu_ai_act high risk: approvals.allow_unattended must be false")
        if not _has_data_governance(manifest):
            errors.append("eu_ai_act: content_screening.enabled or guardrails with input target required")

    if errors:
        raise GovernanceError("; ".join(errors))

    # Not framework-gated: arbitrary subprocess execution is never acceptable.
    assert_stdio_allowed(manifest, settings)
    # Nor is silently routing to a provider the manifest did not permit.
    assert_outbound_providers_allowed(manifest, settings)


__all__ = [
    "GovernanceError",
    "apply_transparency_notice",
    "assert_outbound_providers_allowed",
    "assert_stdio_allowed",
    "transparency_notice_text",
    "validate_for_write",
    "validate_governance",
]
