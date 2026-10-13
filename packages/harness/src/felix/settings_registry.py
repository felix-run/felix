"""The shape operator registries share: a `FELIX_*` JSON map of id -> spec, scoped by tenant.

`FELIX_WEBHOOK_ENDPOINTS` and `FELIX_EGRESS_CREDENTIALS` both let an operator register something
once and a manifest name it by id, never by value. The id grammar, the top-level parse and the
`tenants` rule are one rule each: a manifest validator and the boot parser that read the same ids
must agree on them, and "open to every tenant has to be written out" is a policy, not a detail.
Each registry keeps its own field validation.
"""

from __future__ import annotations

import json
import re
from typing import Any

# What a manifest may name: lowercase, so an id typed in two cases is one id. `\Z`, not `$`, which
# also matches before a trailing newline.
REGISTRY_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}\Z")


def parse_registry(raw: str, *, kind: str, shape: str) -> dict[str, dict[str, Any]]:
    """`raw` as `{id: spec}`, every id valid and every spec an object; `{}` when unset.
    `ValueError` naming what is wrong, in the words of the registry (`kind`, `shape`)."""
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"not valid JSON ({exc.msg})") from None
    if not isinstance(parsed, dict):
        raise ValueError(f"must be a JSON object of {kind} id -> {shape}")
    for name, spec in parsed.items():
        if not isinstance(name, str) or not REGISTRY_ID_RE.match(name):
            raise ValueError(f"{kind} id {name!r} must match {REGISTRY_ID_RE.pattern}")
        if not isinstance(spec, dict):
            raise ValueError(f"{kind} {name!r} must be an object")
    return parsed


def parse_tenant_list(value: Any, *, what: str) -> frozenset[str] | None:
    """A tenant binding: a non-empty list of tenant ids, or `None` for `"*"`. `ValueError` saying
    `what` must be one -- there is no default, so open-to-all is always written out."""
    if value == "*":
        return None
    if not (isinstance(value, list) and value and all(isinstance(t, str) and t for t in value)):
        raise ValueError(f'{what} must be a list of tenant ids, or "*" for all')
    return frozenset(value)


def parse_tenants(spec: dict[str, Any], *, kind: str, name: str) -> frozenset[str] | None:
    """The tenants that may name an entry: a non-empty list, or `None` for `"*"`. Required, with
    `"*"` for every tenant: an entry open to all has to be a written decision, because a forgotten
    key would otherwise make it one."""
    return parse_tenant_list(spec.get("tenants"), what=f"{kind} {name!r} `tenants`")


def tenant_allowed(tenants: frozenset[str] | None, tenant_id: str) -> bool:
    return tenants is None or tenant_id in tenants


def development_install(settings: Any) -> bool:
    """A development install, as `deploy/GOVERNANCE.md` means it: `FELIX_ENVIRONMENT=development`
    *and* `FELIX_AUTH_MODE=none`. The environment alone is not one -- Compose and the settings both
    default it to `development` -- so a gate that loosens anything (literal secrets in a registry,
    say) asks this, never the environment."""
    environment, auth_mode = getattr(settings, "environment", ""), getattr(settings, "auth_mode", "")
    return environment == "development" and auth_mode == "none"


__all__ = [
    "REGISTRY_ID_RE",
    "development_install",
    "parse_registry",
    "parse_tenant_list",
    "parse_tenants",
    "tenant_allowed",
]
