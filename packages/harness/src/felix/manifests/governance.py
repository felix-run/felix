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


__all__ = [
    "GovernanceError",
    "apply_transparency_notice",
    "assert_stdio_allowed",
    "transparency_notice_text",
    "validate_governance",
]
