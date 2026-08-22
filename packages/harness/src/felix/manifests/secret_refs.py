"""Resolve ``secret:NAME`` refs on outbound manifest integration fields."""

from __future__ import annotations

from typing import Any

from felix.manifests.schema import (
    A2APeerRef,
    ContainerRef,
    Manifest,
    McpServerRef,
)
from felix.secrets import (
    SecretsProvider,
    build_secrets,
    is_secret_ref,
    looks_like_plaintext_secret,
    resolve_secret_value,
)


class PlaintextSecretError(ValueError):
    """Raised when a manifest embeds a credential instead of a secret ref."""


def assert_no_plaintext_secrets(manifest: Manifest) -> None:
    """Reject Bearer/Basic/long-token auth and non-ref env values."""
    errors: list[str] = []
    for ref in manifest.spec.mcp:
        if looks_like_plaintext_secret(ref.auth):
            errors.append(f"mcp_servers.{ref.name}.auth looks like a plaintext secret")
        for key, val in (ref.env or {}).items():
            if not val:
                continue
            if not is_secret_ref(val):
                errors.append(
                    f"mcp_servers.{ref.name}.env.{key} must use secret:NAME "
                    "(plaintext env values are forbidden)"
                )
    for ref in manifest.spec.peers:
        if looks_like_plaintext_secret(ref.auth):
            errors.append(f"peers.{ref.name}.auth looks like a plaintext secret")
    for ref in manifest.spec.containers:
        if looks_like_plaintext_secret(ref.auth):
            errors.append(f"containers.{ref.name}.auth looks like a plaintext secret")
    if errors:
        raise PlaintextSecretError("; ".join(errors))


async def resolve_mcp_ref(ref: McpServerRef, provider: SecretsProvider) -> McpServerRef:
    auth = await resolve_secret_value(provider, ref.auth) if ref.auth else ""
    env: dict[str, str] = {}
    for key, val in (ref.env or {}).items():
        env[key] = await resolve_secret_value(provider, val) if val else ""
    return ref.model_copy(update={"auth": auth, "env": env})


async def resolve_peer_ref(ref: A2APeerRef, provider: SecretsProvider) -> A2APeerRef:
    auth = await resolve_secret_value(provider, ref.auth) if ref.auth else ""
    return ref.model_copy(update={"auth": auth})


async def resolve_container_ref(ref: ContainerRef, provider: SecretsProvider) -> ContainerRef:
    auth = await resolve_secret_value(provider, ref.auth) if ref.auth else ""
    return ref.model_copy(update={"auth": auth})


async def resolve_outbound_secrets(
    manifest: Manifest,
    settings: Any | None,
) -> tuple[list[McpServerRef], list[A2APeerRef], list[ContainerRef]]:
    """Return copies of MCP/peer/container refs with secrets resolved.

    The source manifest is unchanged so ``manifest_json`` never stores values.
    """
    if settings is None:
        return list(manifest.spec.mcp), list(manifest.spec.peers), list(manifest.spec.containers)
    provider = build_secrets(settings)
    mcp = [await resolve_mcp_ref(r, provider) for r in manifest.spec.mcp]
    peers = [await resolve_peer_ref(r, provider) for r in manifest.spec.peers]
    containers = [await resolve_container_ref(r, provider) for r in manifest.spec.containers]
    return mcp, peers, containers


__all__ = [
    "PlaintextSecretError",
    "assert_no_plaintext_secrets",
    "resolve_container_ref",
    "resolve_mcp_ref",
    "resolve_outbound_secrets",
    "resolve_peer_ref",
]
