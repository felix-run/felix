"""`plugin.json` at a skill bundle's root, and its per-skill egress allowlist."""

from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

# One optional leftmost-wildcard label, then two or more dot-separated labels. Accepts
# `api.stripe.com`, `*.example.com`; rejects catch-alls (`*`, `*.*`) and TLD-wide
# wildcards (`*.com`), which would let a skill self-grant effectively unrestricted egress.
_HOST_PATTERN = re.compile(r"^(\*\.)?[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+\Z")

_HOST_MESSAGE = (
    "must be a concrete host or specific wildcard (e.g. api.example.com or *.example.com); "
    "catch-all and TLD-wide patterns are not allowed"
)


def is_allowed_host_pattern(host: object) -> bool:
    """True when ``host`` is a safe per-skill egress allowlist entry."""
    if not isinstance(host, str):
        return False
    h = host.strip().lower()
    if not h or len(h) > 253:
        return False
    return bool(_HOST_PATTERN.match(h))


class McpServer(BaseModel):
    name: str
    command: str | None = None
    url: str | None = None

    @field_validator("url")
    @classmethod
    def _url(cls, value: str | None) -> str | None:
        if value is None:
            return value
        parts = urlsplit(value)
        if not parts.scheme or not (parts.netloc or parts.path):
            raise ValueError("Invalid url")
        return value


class McpSpec(BaseModel):
    servers: list[McpServer] | None = None


class NetworkSpec(BaseModel):
    model_config = ConfigDict(validate_by_name=True, validate_by_alias=True)

    allowed_hosts: list[str] | None = Field(default=None, alias="allowedHosts", max_length=50)

    @field_validator("allowed_hosts")
    @classmethod
    def _hosts(cls, value: list[str] | None) -> list[str] | None:
        for host in value or []:
            if not is_allowed_host_pattern(host):
                raise ValueError(f"{host!r} {_HOST_MESSAGE}")
        return value


class PluginManifest(BaseModel):
    """`plugin.json`. Unknown keys are ignored, as the zod schema it mirrors strips them."""

    name: str = Field(min_length=1, max_length=128)
    version: str | None = None
    description: str | None = None
    skills: list[str] = Field(default_factory=lambda: ["SKILL.md"])
    agents: list[str] | None = None
    rules: list[str] | None = None
    mcp: McpSpec | None = None
    network: NetworkSpec | None = None

    @field_validator("agents")
    @classmethod
    def _agents(cls, value: list[str] | None) -> list[str] | None:
        for agent in value or []:
            if not 1 <= len(agent) <= 64:
                raise ValueError("agent names must be 1-64 characters")
        return value


def parse_plugin_manifest(raw: str) -> PluginManifest | None:
    """The parsed manifest, or None when ``raw`` is not JSON or does not validate."""
    try:
        return PluginManifest.model_validate(json.loads(raw))
    except ValueError, ValidationError:
        return None


__all__ = ["PluginManifest", "is_allowed_host_pattern", "parse_plugin_manifest"]
