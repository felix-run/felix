"""`FELIX_AUTH_API_KEYS`: the one parser, read at boot and at the door.

There were two readers. `validate_runtime` checked the shape, and the request path read the raw
JSON again with its own defaults, so the two could disagree about what a key grants -- a string
`scopes` iterated into its characters at the door whatever the boot check thought of it. Both now
read `parse_api_keys`, and the defaults below are the only ones.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

#: The tenant of a key whose entry names none.
DEFAULT_API_KEY_TENANT = "default"
#: The subject of a key whose entry names none.
DEFAULT_API_KEY_SUBJECT = "api_key"


@dataclass(frozen=True, slots=True)
class ApiKeyEntry:
    tenant_id: str = DEFAULT_API_KEY_TENANT
    #: The entry's own `sub`, or None: a key without one has no personal library.
    sub: str | None = None
    scopes: frozenset[str] = frozenset()
    #: The entry as configured, for `AuthContext.raw_claims`.
    claims: dict[str, Any] = field(default_factory=dict)

    @property
    def subject(self) -> str:
        return self.sub or DEFAULT_API_KEY_SUBJECT


def _optional_str(entry: dict[str, Any], name: str, label: str) -> str | None:
    value = entry.get(name)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError(f'{label}: "{name}" must be a string, not {type(value).__name__}')
    return value


def parse_api_keys(raw: str) -> dict[str, ApiKeyEntry]:
    """Key -> entry. Raises `ValueError` on any shape error.

    Messages name an entry by its position and never by its key, and a JSON error by line and
    column, never by `exc.doc`: the document is the credential list.
    """
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"is not valid JSON (line {exc.lineno}, column {exc.colno}); expected "
            '{"<key>": {"tenant_id": "...", "sub": "...", "scopes": ["..."]}}'
        ) from None
    if not isinstance(data, dict):
        raise ValueError("must be a JSON object mapping each key to its entry")
    keys: dict[str, ApiKeyEntry] = {}
    for index, (key, entry) in enumerate(data.items(), start=1):
        label = f"entry {index}"
        if not isinstance(entry, dict):
            raise ValueError(f"{label} must be a JSON object, not {type(entry).__name__}")
        scopes = entry.get("scopes")
        if scopes is None:
            scopes = []
        if not isinstance(scopes, list) or not all(isinstance(s, str) for s in scopes):
            raise ValueError(
                f'{label}: "scopes" must be a list of strings, such as ["admin"], not {type(scopes).__name__}'
            )
        keys[key] = ApiKeyEntry(
            tenant_id=_optional_str(entry, "tenant_id", label) or DEFAULT_API_KEY_TENANT,
            sub=_optional_str(entry, "sub", label),
            scopes=frozenset(scopes),
            claims=dict(entry),
        )
    return keys


__all__ = ["DEFAULT_API_KEY_SUBJECT", "DEFAULT_API_KEY_TENANT", "ApiKeyEntry", "parse_api_keys"]
