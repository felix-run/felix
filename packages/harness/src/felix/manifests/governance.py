"""Compile-time governance validation and transparency helpers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from felix.manifests.schema import Manifest, any_limit, child_agent_names
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


def assert_stdio_env_allowed(manifest: Manifest) -> None:
    """Refuse a stdio MCP server whose ``env`` sets a variable that runs code (`env_key_refused`).

    On write only. At spawn the same check drops that one server's tools; refusing it at compile
    too would fail the whole agent of every manifest already stored with such a key, which is the
    outage a narrowed field causes (see CLAUDE.md on `compat.RETIRED`).
    """
    from felix.security.stdio_policy import env_key_refused

    errors = [
        f"mcp_servers[{ref.name}]: env may not set {key}"
        for ref in manifest.spec.mcp or []
        if ref.transport == "stdio"
        for key in ref.env
        if env_key_refused(key)
    ]
    if errors:
        raise GovernanceError("; ".join(errors))


Route = tuple[str, str]  # (the field that names it, the route id)


def _allowed_providers(manifest: Manifest) -> set[str]:
    """`auth.outbound.providers`, normalised; empty means unpinned."""
    return {str(p).strip().lower() for p in (manifest.spec.auth.outbound.providers or []) if str(p).strip()}


def _cost_cap_declared(manifest: Manifest) -> bool:
    """An explicitly declared `limits.max_cost_usd`; the filled-in default does not count."""
    limits = getattr(manifest.spec, "limits", None)
    return limits is not None and getattr(limits, "max_cost_usd", None) is not None


def _named(pairs: list[tuple[str, str | None]]) -> list[Route]:
    return [(field, rid.strip()) for field, rid in pairs if rid and rid.strip()]


def _routes_priced_by_spec_model(manifest: Manifest, settings: Any) -> list[Route]:
    """Routes built from `spec.model` with only the id swapped, so `spec.model.price` rates them.

    Built by `build_model`/`build_one_model(settings, spec, id)` (fallbacks, vision, escalation,
    capture), `_spec_with_model` (planner, executor) and `model_copy` (`jobs/heartbeat.py`).
    """
    from felix.patterns.model_vision import vision_plan

    spec = manifest.spec.model
    pairs: list[tuple[str, str | None]] = [("spec.model.id", spec.id or settings.default_model_id)]
    pairs += [(f"spec.model.fallbacks[{n}]", f) for n, f in enumerate(spec.fallbacks)]
    if spec.vision_model:
        pairs.append(("spec.model.vision_model", spec.vision_model))
    else:
        # A text-only primary hands images to the deployment's default vision route, which the
        # author never named but the run still sends its conversation to.
        pairs.append(("FELIX_DEFAULT_VISION_MODEL_ID", vision_plan(settings, spec).vision_id))
    escalation = spec.confidence_escalation
    if escalation.enabled:
        pairs.append(("spec.model.confidence_escalation.escalate_to", escalation.escalate_to))
    plan = manifest.spec.plan_execute
    pairs += [("spec.plan_execute.planner_model", plan.planner_model)]
    pairs += [("spec.plan_execute.executor_model", plan.executor_model)]
    if manifest.spec.heartbeat is not None:
        pairs.append(("spec.heartbeat.model", manifest.spec.heartbeat.model))
    capture = manifest.spec.memory.capture
    if capture.enabled:
        pairs.append(("spec.memory.capture.model", capture.model))
    return _named(pairs)


def _routes_at_catalog_rates(manifest: Manifest) -> list[Route]:
    """Routes built from a bare `ModelSpec(id=...)`: billed at catalog rates whatever
    `spec.model.price` says, and each one is sent content from the run.

    With `_routes_priced_by_spec_model`, the one inventory both checks below read, so a new
    model field is added once; `tests/unit/test_inert_manifest_fields.py` fails a schema field
    that names a route and is in neither. Before it existed a manifest pinned to one provider
    could send tool output to another through a judge or the screening model.
    """
    spec = manifest.spec
    pairs: list[tuple[str, str | None]] = [
        (f"spec.http_tools[{ref.name}].extract_model", ref.extract_model) for ref in spec.http_tools
    ]
    pairs.append(("spec.reflect.verifier_model", spec.reflect.verifier_model))
    pairs += [(f"spec.guardrails.judges[{rule.name}].model", rule.model) for rule in spec.guardrails.judges]
    if spec.content_screening.enabled:
        pairs.append(("spec.content_screening.model", spec.content_screening.model))
        pairs.append(("spec.content_screening.image_model", spec.content_screening.image_model))
    if spec.memory.consolidate.enabled:
        pairs.append(("spec.memory.consolidate.model", spec.memory.consolidate.model))
    return _named(pairs)


def assert_outbound_providers_allowed(manifest: Manifest, settings: Any | None = None) -> None:
    """Enforce ``spec.auth.outbound.providers`` on every model route the manifest names.

    The field was declared and never read, so a manifest naming ``[anthropic]`` could
    still route to OpenAI or Workers AI. Checked at compile so a violation fails the
    build with a clear message rather than at the first model call.
    """
    allowed = _allowed_providers(manifest)
    if not allowed:
        return

    from felix.config import get_settings
    from felix.patterns.model import parse_model_routes

    settings = settings or get_settings()
    routes = parse_model_routes(settings)
    wanted = _routes_priced_by_spec_model(manifest, settings) + _routes_at_catalog_rates(manifest)

    errors: list[str] = []
    for field, logical_id in wanted:
        route = routes.get(logical_id)
        if route is None:
            continue  # unknown ids are reported by the model layer, not here
        if route.provider.lower() not in allowed:
            errors.append(
                f"{field} {logical_id!r} routes to provider {route.provider!r}, "
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
    `max_cost_usd` from `ABSOLUTE_LIMITS`, and refusing on that would break every deployment
    on an unpriced model for a ceiling the author never asked for. Declaring one is a
    statement that spend matters here, and that is the statement Felix must not fake.

    The escape hatch is `spec.model.price`, for the routes built from `spec.model`. Every
    other route is billed at catalog rates, so it must route to a priced model.
    """
    if not _cost_cap_declared(manifest):
        return
    priced_by_manifest = bool(manifest.spec.model.price)
    catalog = _routes_at_catalog_rates(manifest)
    if priced_by_manifest and not catalog:
        return  # spec.model.price covers every route left

    from felix.config import get_settings
    from felix.model_catalog import is_priced
    from felix.patterns.model import parse_model_routes

    settings = settings or get_settings()
    routes = parse_model_routes(settings)
    wanted = catalog if priced_by_manifest else _routes_priced_by_spec_model(manifest, settings) + catalog

    unpriced: list[str] = []
    for field, logical_id in wanted:
        route = routes.get(logical_id)
        if route is None:
            continue  # unknown ids are reported by the model layer, not here
        if not is_priced(route.model):
            at_catalog = " (billed at catalog rates)" if (field, logical_id) in catalog else ""
            unpriced.append(f"{field} {logical_id} -> {route.model}{at_catalog}")
    if unpriced:
        raise GovernanceError(
            f"limits.max_cost_usd is declared but Felix has no rates for {', '.join(unpriced)}, "
            "so the cap could not be enforced. Supply rates with spec.model.price, which covers "
            "spec.model and the routes built from it; route anything billed at catalog rates to a "
            "priced model; or drop the limit."
        )


RequestModelRefusal = Literal["model_provider_not_allowed", "model_unpriced", "model_not_priced_by_manifest"]


def request_model_refusal(manifest: Manifest, model_id: str, settings: Any) -> RequestModelRefusal | None:
    """Why a request's `model` override may not run on this manifest, as a wire code, or None.

    The override replaces `spec.model.id` for one turn (the planner's too), so the manifest's own
    rules apply to it: `auth.outbound.providers` was otherwise a statement any `chat:write`
    caller could route around with one field. Under a declared `limits.max_cost_usd` it must be
    priced. The override keeps `spec.model.price`, which the author set for the routes the
    manifest names, so with one set the override must be one of those; any other route would be
    metered at rates chosen for a different model.
    """
    from felix.model_catalog import is_priced
    from felix.patterns.model import parse_model_routes

    route = parse_model_routes(settings).get(model_id)
    if route is None:
        return None  # unknown ids are reported by the model layer, not here
    allowed = _allowed_providers(manifest)
    if allowed and route.provider.lower() not in allowed:
        return "model_provider_not_allowed"
    if not _cost_cap_declared(manifest):
        return None
    if manifest.spec.model.price:
        named = {rid for _, rid in _routes_priced_by_spec_model(manifest, settings)}
        return None if model_id in named else "model_not_priced_by_manifest"
    return None if is_priced(route.model) else "model_unpriced"


async def request_model_tree_refusal(
    manifest: Manifest,
    model_id: str,
    settings: Any,
    *,
    tenant_id: str,
    children: Mapping[str, Manifest | None] | None = None,
) -> RequestModelRefusal | None:
    """`request_model_refusal` for the manifest and every sub-agent the override reaches.

    `_child_input` hands the override to each child turn, and each child compiles from its own
    manifest, so an unpinned router could otherwise carry a caller's route into a child pinned
    to another provider. `children` is the tree a pin check already resolved; when it is empty
    the tree is resolved here, the same walk `build_agent` makes.
    """
    refusal = request_model_refusal(manifest, model_id, settings)
    if refusal is not None or not child_agent_names(manifest.spec):
        return refusal
    tree: dict[str, Manifest | None] = dict(children or {})
    if not tree:
        from felix.manifests.pin import sub_agents_hash

        await sub_agents_hash(settings, tenant_id, manifest, resolved_out=tree)
    for child in tree.values():
        if child is not None and (refusal := request_model_refusal(child, model_id, settings)) is not None:
            return refusal
    return None


def validate_for_run(manifest: Manifest, settings: Any | None = None) -> None:
    """Every check a manifest must pass before anything runs a model on its behalf, in one place.

    The build makes these before compiling a turn. A route that builds `spec.model` without
    compiling (a side question, `/chat/compact`, a rewind's summary) makes them too, or the
    provider pin and the measurable cost cap would hold for turns and not for those calls.
    """
    validate_governance(manifest, settings)
    assert_cost_limit_is_measurable(manifest, settings)


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
    assert_stdio_env_allowed(manifest)
    if any(rule.when_args for rule in manifest.spec.approvals):
        from felix.config import get_settings
        from felix.manifests.approval_args import offline_tools, when_args_problems

        # A `when_args` name the gated tool does not take is a rule that never fires. Refused
        # for tools whose schemas ship with the harness; the rest are warned about at compile.
        problems = when_args_problems(manifest.spec.approvals, offline_tools(settings or get_settings()))
        if problems:
            raise GovernanceError("; ".join(problems))
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
    from felix.security.shell_policy import ShellNotAllowedError, assert_shell_commands_allowed

    try:
        assert_shell_commands_allowed(
            manifest.spec.shell_tools, settings, scope=manifest.spec.workspace.scope
        )
    except ShellNotAllowedError as exc:
        raise GovernanceError(str(exc)) from exc
    # The cowork precedent, as a rule instead of a manifest comment: a tool that execs on the
    # host is reachable by whoever can call the manifest, and the approvals that would gate
    # it are anonymous too when the caller is. Development keeps `make dev` usable.
    env = getattr(settings, "environment", "development") if settings is not None else "development"
    if manifest.spec.shell_tools and manifest.spec.auth.inbound.allow_anonymous and env != "development":
        raise GovernanceError(
            "shell_tools require an authenticated caller: set auth.inbound.allow_anonymous to false"
        )


def manifest_warnings(manifest: Manifest) -> list[str]:
    """What an author should hear about a manifest the store would take anyway.

    The counterpart of `validate_for_write`: those refuse, these never do. `PUT /manifests`
    returns them as `warnings` and `felix validate-manifest` prints them, so the two surfaces an
    author writes through say the same thing. A warning lives here, not as a refusal, when a
    stored manifest carrying it is unwise rather than broken — an upgrade must not make it
    unstorable.
    """
    from felix.jobs.heartbeat import cap_warnings
    from felix.manifests.delete_gate import delete_gate_warnings

    return [*delete_gate_warnings(manifest), *cap_warnings(manifest)]


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
    "assert_stdio_env_allowed",
    "manifest_warnings",
    "transparency_notice_text",
    "validate_for_run",
    "validate_for_write",
    "validate_governance",
]
