"""Shared agent resolve + build helpers (used by API routes and A2A)."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from felix.config import Settings
from felix.manifests.builder import BuildDeps, build_agent
from felix.manifests.inbound_auth import enforce_inbound_auth
from felix.manifests.pin import ensure_thread_pin
from felix.manifests.resolver import ResolvedManifest, resolve_manifest
from felix.manifests.schema import Manifest
from felix.manifests.store import PostgresManifestStore
from felix.patterns.types import Agent
from felix.session.store import build_checkpointer, validate_checkpointer_config
from felix.session.strategies import get_session_strategy
from felix.tools.provider import ToolProvider

logger = logging.getLogger("felix.runtime")


async def resolve_tenant_manifest(
    settings: Settings,
    tenant_id: str,
    name: str,
    *,
    thread_id: str | None = None,
    pin_version: int | None = None,
) -> ResolvedManifest:
    return await resolve_manifest(
        settings,
        tenant_id,
        name,
        thread_id=thread_id,
        # A caller that names a version means it: evaluation scoring a canary has to load
        # that canary, not whatever is active. Unknown versions raise rather than falling
        # back, because a silent fall-back is what made a canary look benchmarked.
        pin_version=pin_version,
        # Under `bundled` the layers above the image do not exist, so they are not supplied.
        # `_read_tenant_postgres` and `_read_object` already return None for a missing store,
        # which is the same collapse a branch in the resolver would produce — expressed by
        # absence rather than by policy in the deepest function on this path.
        manifest_store=None if settings.bundled_only else PostgresManifestStore(settings),
    )


def _apply_metric_allowlist(manifest: Any) -> None:
    """Carry `spec.observability.metrics` onto the active request context."""
    from felix.context import try_get_context

    names = list(getattr(manifest.spec.observability, "metrics", None) or [])
    ctx = try_get_context()
    if ctx is not None and names:
        ctx.metric_names = frozenset(str(n) for n in names)


async def prepare_tenant_invoke(
    settings: Settings,
    *,
    resolved: ResolvedManifest,
    auth: Any,
    thread_id: str | None = None,
) -> None:
    """Enforce inbound auth + compile pin before building/invoking an agent."""
    enforce_inbound_auth(resolved.manifest, auth)
    _apply_metric_allowlist(resolved.manifest)
    tenant_id = getattr(auth, "tenant_id", None) or "default"
    await ensure_thread_pin(
        settings=settings,
        tenant_id=tenant_id,
        thread_id=thread_id,
        manifest=resolved.manifest,
        version=resolved.version,
        # Filled with the sub-agents the pin check resolved, for the build to reuse.
        resolved_out=resolved.sub_agents,
    )


def _context_window_for_manifest(manifest: Any, strategy_spec: Any, settings: Settings | None = None) -> int:
    """Tokens of context to compact against: the declared value, else the model's own window.

    The field used to default to 128000 and this read `model_fields_set` to tell a written
    value from the default, because a manifest on a 1M-context model otherwise compacted at
    128K minus reserve. The default is `None` now, so the value itself says which it is.

    A manifest that names no model runs on `settings.default_model_id` -- the same fallback
    `patterns.model` takes when it builds the client -- so that is the model whose window
    applies. This used to return 128000 for it instead, which is every bundled manifest
    (`quick`, `cowork`, `deep`, ...): each compacted at 128K on a 200K model, and
    `/v1/models` reported 128K for all of them.
    """
    declared = getattr(strategy_spec, "context_window_tokens", None)
    if declared:
        return int(declared)

    model_spec = getattr(getattr(manifest, "spec", None), "model", None)
    if settings is None:
        from felix.config import get_settings

        settings = get_settings()
    model_id = str(getattr(model_spec, "id", "") or "") or str(settings.default_model_id or "")
    if not model_id:
        return 128000
    window = _route_window(model_id, settings)
    from felix.patterns.model_vision import vision_plan

    # A composed vision route answers every call that carries an image, and once a thread
    # holds one, every later call does. Its history has to fit whichever model answers, so a
    # vision model with the smaller window is the one compacted against -- sized for the
    # primary alone, it was handed a history it could not take.
    vision_id = vision_plan(settings, model_spec).vision_id
    if vision_id:
        window = min(window, _route_window(vision_id, settings))
    return window


def _route_window(model_id: str, settings: Settings) -> int:
    from felix.model_catalog import entry_for
    from felix.patterns.model import parse_model_routes

    # `model_id` here is the logical route name. `claude-sonnet` matched only the loose
    # family key at 200K, so a manifest on the default route compacted against a fifth of
    # the window it pays for; an id matching nothing at all fell to the 128K default.
    route = parse_model_routes(settings).get(model_id)
    return entry_for(route.model if route is not None else model_id).context_window


def session_plumbing(settings: Settings, manifest: Any, tenant_id: str) -> tuple[Any | None, Any]:
    """The session store and strategy a turn of `manifest` reads its history through.

    One place, so anything that renders a thread the way its next turn will (`POST /chat/ask`)
    does it with the same checkpointer, strategy and budgets, not a second copy of them. The
    strategy is the bare one: `build_agent` wraps it in the manifest's replay screening, and so
    must any other renderer. The store is None for `checkpointer: none`, which runs the agent
    with no session state.
    """
    spec = getattr(manifest, "spec", None)
    checkpointer = str(getattr(getattr(spec, "memory", None), "checkpointer", "postgres") or "postgres")
    strategy_spec = getattr(spec, "session", None)
    strategy_name = getattr(strategy_spec, "strategy", "full_replay") if strategy_spec else "full_replay"
    memory_spec = getattr(spec, "memory", None)
    validate_checkpointer_config(
        checkpointer,
        session_strategy=strategy_name,
        compact_after_turn=bool(getattr(strategy_spec, "compact_after_turn", False)),
        memory_capture=bool(getattr(getattr(memory_spec, "capture", None), "enabled", False)),
        memory_recall_tools=bool(getattr(getattr(memory_spec, "recall", None), "tools", False)),
    )
    session_store = build_checkpointer(checkpointer, settings, tenant_id=tenant_id)
    strategy = get_session_strategy(
        strategy_name,
        reserve_tokens=int(getattr(strategy_spec, "reserve_tokens", 16384) or 16384),
        keep_recent_tokens=int(getattr(strategy_spec, "keep_recent_tokens", 20000) or 20000),
        context_window_tokens=_context_window_for_manifest(manifest, strategy_spec, settings),
        compaction_enabled=bool(getattr(strategy_spec, "compaction_enabled", True)),
    )
    return session_store, strategy


def default_object_store(settings: Settings) -> Any | None:
    """The deployment's object store, or None — logged — when it cannot be opened."""
    try:
        from felix.storage import get_object_store

        # Cached, not built per request: S3ObjectStore opens a client it never
        # closed, so a fresh store per chat leaked one every time.
        return get_object_store(settings)
    except Exception:
        # Silently swallowing this meant SYSTEM.md, AGENTS.md, instruction files and
        # object-store skills all vanished and the agent fell back to
        # f"You are {name}." — a misconfigured bucket quietly removed the prompt.
        logger.error(
            "object store unavailable; system prompt files and object-store skills will not be loaded",
            exc_info=True,
        )
        return None


async def build_tenant_agent(
    settings: Settings,
    *,
    manifest: Any,
    tools: ToolProvider,
    tenant_id: str,
    skill_owner: str | None,
    object_store: Any | None = None,
    workspace_root: str | None = None,
    load_agents_md: bool = False,
    sub_agents: Mapping[str, Manifest | None] | None = None,
) -> Agent:
    """Compile `manifest` for `tenant_id`.

    `skill_owner` is the caller's personal skill library (`AuthContext.skill_owner`), loaded
    when the manifest sets `spec.personal_skills`; None loads the tenant's alone. It has no
    default: a request path that forgot it would compile without the caller's skills and look
    like it worked, and a resumed fiber that forgot it would differ from the request that
    started it, so every call site says which it means.

    `sub_agents` is `ResolvedManifest.sub_agents` — the children a pin check resolved for this
    request. Every caller that ran `prepare_tenant_invoke` passes it, so a pinned thread runs
    the sub-agents its pin verified rather than whatever resolves a moment later;
    `tests/unit/test_invariants.py` holds the call sites to it.
    """
    session_store, session_strategy = session_plumbing(settings, manifest, tenant_id)
    store = object_store if object_store is not None else default_object_store(settings)

    deps = BuildDeps(
        tools=tools,
        settings=settings,
        session_store=session_store,
        session_strategy=session_strategy,
        object_store=store,
        tenant_id=tenant_id,
        skill_owner=skill_owner,
        workspace_root=workspace_root or getattr(settings, "workspace_root", None) or None,
        load_agents_md=load_agents_md or bool(getattr(settings, "load_agents_md", False)),
    )
    deps.sub_agent_builder = _tenant_sub_agent_builder(settings, tenant_id, deps, sub_agents or {})
    return await build_agent(manifest, deps=deps, settings=settings)


def _tenant_sub_agent_builder(
    settings: Settings, tenant_id: str, deps: BuildDeps, checked: Mapping[str, Manifest | None]
) -> Any:
    """Compile each child agent -- `spec.sub_agents` and `spec.delegation` alike -- as this
    tenant would reach it by name.

    Store, then object store, then bundled — the order a request resolves a manifest in. It
    used to be bundled YAML only, so a router whose children were the tenant's own agents
    compiled them as empty `You are <name>.` manifests and routed to those. A name that
    resolves nowhere is a `LookupError`, which fails the compile rather than the conversation.
    """

    async def build(name: str) -> Agent:
        if name in checked:
            # The pin check resolved this name for this request. Compiling that, not a fresh
            # resolution, is what makes the checked tree the running one: a child activated
            # between the two used to compile for one turn under a pin that verified its
            # predecessor. Found nowhere at check time stays found nowhere.
            child = checked[name]
            if child is None:
                raise LookupError(f"Unknown sub-agent manifest: {name}")
            return await build_agent(child, deps=deps, settings=settings)
        resolved = await resolve_tenant_manifest(settings, tenant_id, name)
        return await build_agent(resolved.manifest, deps=deps, settings=settings)

    return build


__all__ = [
    "build_tenant_agent",
    "default_object_store",
    "prepare_tenant_invoke",
    "resolve_tenant_manifest",
    "session_plumbing",
]
