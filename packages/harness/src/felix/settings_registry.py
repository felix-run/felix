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


def parse_tenants(spec: dict[str, Any], *, kind: str, name: str) -> frozenset[str] | None:
    """The tenants that may name an entry: a non-empty list, or `None` for `"*"`. Required, with
    `"*"` for every tenant: an entry open to all has to be a written decision, because a forgotten
    key would otherwise make it one."""
    tenants = spec.get("tenants")
    if tenants == "*":
        return None
    if not (isinstance(tenants, list) and tenants and all(isinstance(t, str) and t for t in tenants)):
        raise ValueError(f'{kind} {name!r} `tenants` must be a list of tenant ids, or "*" for all')
    return frozenset(tenants)


def tenant_allowed(tenants: frozenset[str] | None, tenant_id: str) -> bool:
    return tenants is None or tenant_id in tenants


__all__ = ["REGISTRY_ID_RE", "parse_registry", "parse_tenants", "tenant_allowed"]
