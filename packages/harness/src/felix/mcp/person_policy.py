"""Which MCP servers may be sent a person's token: `FELIX_MCP_PERSON_AUTH`, deny by default.

`mcp_servers[].auth: person:<provider>` sends the token of whoever the agent runs for to the URL
the manifest names, and a manifest is written by whoever holds `manifests:write` in the tenant --
not by the people whose tokens it would send. So the destination is the operator's decision, as a
`secret:NAME` ref's is (`felix.secret_policy`): per provider, the exact https servers that may
receive its tokens, and the tenants whose manifests may name each.

    FELIX_MCP_PERSON_AUTH={"github": [{"url": "https://api.githubcopilot.com/mcp/", "tenants": ["acme"]}]}

A server matches an entry when its scheme, host and port are the entry's and its path lies under
the entry's path (at a `/` boundary). Unset, or a provider not listed, and no server receives that
provider's tokens. Checked when a manifest is stored (`secret_refs.assert_manifest_secrets_permitted`)
and again at compile (`person_auth.open_person_refs`), for rows stored before an entry was removed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit

from felix.secrets import SecretNotPermittedError
from felix.settings_registry import parse_tenant_list, tenant_allowed


class PersonAuthNotPermittedError(SecretNotPermittedError):
    """A manifest's `auth: person:<provider>` server is not one `FELIX_MCP_PERSON_AUTH` allows for
    its tenant. Its message, written here, names the server and the setting, never a token."""


@dataclass(frozen=True, slots=True)
class _Server:
    origin: tuple[str, str, int]
    path: str
    tenants: frozenset[str] | None


def _origin(url: str, plain: bool = False) -> tuple[tuple[str, str, int], str]:
    """(scheme, host, port) and path. `plain` admits `http`, for the development exemption only."""
    parts = urlsplit(url)
    schemes = {"https", "http"} if plain else {"https"}
    if parts.scheme not in schemes or not parts.hostname or parts.username or parts.password:
        raise ValueError(f"{url!r} is not an https URL without credentials")
    # The path is compared as text, and the client sends it after removing dot segments
    # (`/mcp/acme/../evil` goes to `/evil`): a dot segment, or a dot or slash in escapes a server
    # may decode, would step out of an entry's path. Refused rather than normalised.
    lowered = parts.path.lower()
    if any(seg in {".", ".."} for seg in parts.path.split("/")) or any(
        esc in lowered for esc in ("%2e", "%2f", "%5c")
    ):
        raise ValueError(f"{url!r} has a dot segment or an escaped dot or slash in its path")
    if parts.query or parts.fragment:
        raise ValueError(f"{url!r} carries a query or fragment")
    default_port = 443 if parts.scheme == "https" else 80
    return (parts.scheme, parts.hostname.lower(), parts.port or default_port), parts.path or "/"


@lru_cache(maxsize=4)
def _parse(raw: str, plain: bool = False) -> dict[str, tuple[_Server, ...]]:
    if not raw.strip():
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError('must be a JSON object: {"<provider>": [{"url": ..., "tenants": [...]}]}')
    out: dict[str, tuple[_Server, ...]] = {}
    for provider, entries in data.items():
        if not isinstance(entries, list) or not entries:
            raise ValueError(f"{provider!r} must list at least one server")
        servers = []
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"url", "tenants"}:
                raise ValueError(f"each {provider!r} entry is {{url, tenants}}")
            origin, path = _origin(str(entry["url"]), plain)
            tenants = parse_tenant_list(entry["tenants"], what=f"{provider!r} tenants")
            servers.append(_Server(origin, path, tenants))
        out[str(provider)] = tuple(servers)
    return out


def _plain(settings: Any) -> bool:
    # The development exemption every outbound integration gets, and nothing wider.
    return getattr(settings, "environment", "") == "development" and bool(
        getattr(settings, "allow_insecure", False)
    )


def parse_person_auth(settings: Any) -> dict[str, tuple[_Server, ...]]:
    """The setting, parsed; `ValueError` for a malformed one (refused at boot)."""
    return _parse(getattr(settings, "mcp_person_auth", "") or "", _plain(settings))


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix if prefix.endswith("/") else prefix + "/")


def person_auth_permitted(provider: str, url: str, tenant_id: str, settings: Any) -> bool:
    try:
        origin, path = _origin(url, _plain(settings))
        servers = parse_person_auth(settings).get(provider, ())
    except ValueError:
        return False
    return any(
        s.origin == origin and _under(path, s.path) and tenant_allowed(s.tenants, tenant_id) for s in servers
    )


def assert_person_auth_permitted(manifest: Any, tenant_id: str, settings: Any) -> None:
    """`PersonAuthNotPermittedError` for a manifest sending a person's token where the operator has
    not said it may go."""
    for ref in manifest.spec.mcp:
        provider = ref.person_provider
        if provider and not person_auth_permitted(provider, ref.url, tenant_id, settings):
            raise PersonAuthNotPermittedError(
                f"mcp_servers.{ref.name} sends the caller's {provider} token to a server "
                f"FELIX_MCP_PERSON_AUTH does not allow for this tenant"
            )


__all__ = [
    "PersonAuthNotPermittedError",
    "assert_person_auth_permitted",
    "parse_person_auth",
    "person_auth_permitted",
]
