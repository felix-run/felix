"""Shared agent resolve + build helpers (used by API routes and A2A)."""

from __future__ import annotations

from typing import Any

from felix.config import Settings
from felix.manifests.builder import BuildDeps, build_agent
from felix.manifests.inbound_auth import enforce_inbound_auth
from felix.manifests.pin import ensure_thread_pin
from felix.manifests.resolver import ResolvedManifest, resolve_manifest
from felix.manifests.store import PostgresManifestStore
from felix.patterns.types import Agent
from felix.session.store import get_session_store
from felix.session.strategies import get_session_strategy
from felix.tools.provider import ToolProvider


async def resolve_tenant_manifest(
    settings: Settings,
    tenant_id: str,
    name: str,
    *,
    thread_id: str | None = None,
) -> ResolvedManifest:
    return await resolve_manifest(
        settings,
        tenant_id,
        name,
        thread_id=thread_id,
        manifest_store=PostgresManifestStore(settings),
    )


async def prepare_tenant_invoke(
    settings: Settings,
    *,
    resolved: ResolvedManifest,
    auth: Any,
    thread_id: str | None = None,
) -> None:
    """Enforce inbound auth + compile pin before building/invoking an agent."""
    enforce_inbound_auth(resolved.manifest, auth)
    tenant_id = getattr(auth, "tenant_id", None) or "default"
    await ensure_thread_pin(
        settings=settings,
        tenant_id=tenant_id,
        thread_id=thread_id,
        manifest=resolved.manifest,
        version=resolved.version,
    )


async def build_tenant_agent(
    settings: Settings,
    *,
    manifest: Any,
    tools: ToolProvider,
    tenant_id: str,
    object_store: Any | None = None,
    workspace_root: str | None = None,
    load_agents_md: bool = False,
) -> Agent:
    session_store = get_session_store(settings, tenant_id=tenant_id)
    strategy_spec = getattr(getattr(manifest, "spec", None), "session", None)
    strategy_name = getattr(strategy_spec, "strategy", "full_replay") if strategy_spec else "full_replay"
    reserve = int(getattr(strategy_spec, "reserve_tokens", 16384) or 16384)
    keep_recent = int(getattr(strategy_spec, "keep_recent_tokens", 20000) or 20000)
    context_window = int(getattr(strategy_spec, "context_window_tokens", 128000) or 128000)
    compaction_enabled = bool(getattr(strategy_spec, "compaction_enabled", True))

    store = object_store
    if store is None:
        try:
            from felix.storage import build_object_store

            store = build_object_store(settings)
        except Exception:
            store = None

    deps = BuildDeps(
        tools=tools,
        settings=settings,
        session_store=session_store,
        session_strategy=get_session_strategy(
            strategy_name,
            reserve_tokens=reserve,
            keep_recent_tokens=keep_recent,
            context_window_tokens=context_window,
            compaction_enabled=compaction_enabled,
        ),
        object_store=store,
        tenant_id=tenant_id,
        workspace_root=workspace_root or getattr(settings, "workspace_root", None) or None,
        load_agents_md=load_agents_md or bool(getattr(settings, "load_agents_md", False)),
    )
    return await build_agent(manifest, deps=deps, settings=settings)


__all__ = ["build_tenant_agent", "prepare_tenant_invoke", "resolve_tenant_manifest"]
