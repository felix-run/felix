"""Shared agent resolve + build helpers (used by API routes and A2A)."""

from __future__ import annotations

from typing import Any

from felix.config import Settings
from felix.manifests.builder import BuildDeps, build_agent
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


async def build_tenant_agent(
    settings: Settings,
    *,
    manifest: Any,
    tools: ToolProvider,
    tenant_id: str,
) -> Agent:
    session_store = get_session_store(settings, tenant_id=tenant_id)
    strategy_spec = getattr(getattr(manifest, "spec", None), "session", None)
    strategy_name = (
        getattr(strategy_spec, "strategy", "full_replay") if strategy_spec else "full_replay"
    )
    deps = BuildDeps(
        tools=tools,
        settings=settings,
        session_store=session_store,
        session_strategy=get_session_strategy(strategy_name),
    )
    return await build_agent(manifest, deps=deps, settings=settings)


__all__ = ["build_tenant_agent", "resolve_tenant_manifest"]
