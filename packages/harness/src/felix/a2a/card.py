"""A2A agent card builder."""

from __future__ import annotations

from typing import Any

from felix.manifests.schema import Manifest


def build_agent_card(
    manifest: Manifest,
    *,
    base_url: str = "",
    mcp_enabled: bool = True,
) -> dict[str, Any]:
    name = manifest.metadata.name
    description = manifest.metadata.description or f"Felix agent {name}"
    card: dict[str, Any] = {
        "name": name,
        "description": description,
        "url": f"{base_url.rstrip('/')}/chat" if base_url else "",
        "version": manifest.metadata.version,
        "capabilities": {
            "streaming": True,
            "mcp": mcp_enabled,
        },
        "skills": [],
    }
    return card
