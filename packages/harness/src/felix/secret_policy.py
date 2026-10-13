"""Which secrets a manifest may resolve: `FELIX_MANIFEST_SECRETS`, and what it can never open.

Every secrets backend resolves any name the process can see, and a manifest chooses both the
name and where the value is sent (an MCP server's URL, a peer's, a gateway's, GitHub). So a
manifest's `secret:NAME` refs are an allowlist per tenant, in front of the backend's `get`:

- Felix's own secrets are refused always: any `FELIX_*` name, every candidate name in the
  hydrate map, and the deployment's infrastructure credentials (`is_reserved_secret_name`).
- A secret an operator registry already binds (an egress credential, a webhook's signing key, …)
  cannot also be opened to manifests; boot refuses the overlap (`assert_manifest_secrets_unbound`).
- Anything else must be listed for the tenant, or for `"*"` (`assert_manifest_secret_permitted`).

`tenant_secrets_provider` is the backend as one tenant's manifests may use it. Every resolve of
a manifest-supplied name goes through it; nothing under `felix/manifests/` names `build_secrets`
(`tests/unit/test_invariants.py`).
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from felix.logging_setup import loggable
from felix.secrets import (
    SecretNotPermittedError,
    SecretsProvider,
    build_secrets,
    hydrated_secret_names,
    secret_ref_name,
)
from felix.settings_registry import parse_tenant_list, tenant_allowed

# Credentials the deployment itself runs on that live outside `FELIX_*` and outside the hydrate
# map: the database and cache URLs, the Compose stack's Postgres and MinIO passwords, the
# Cloudflare Tunnel token, the `gh` CLI's token, and the process's own cloud credentials.
_INFRA_SECRET_NAMES = frozenset(
    {
        "DATABASE_URL",
        "REDIS_URL",
        "POSTGRES_PASSWORD",
        "MINIO_ROOT_USER",
        "MINIO_ROOT_PASSWORD",
        "PGPASSWORD",
        "TUNNEL_TOKEN",
        "GH_TOKEN",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI",
        "AWS_CONTAINER_AUTHORIZATION_TOKEN",
        "GOOGLE_APPLICATION_CREDENTIALS",
    }
)


def _normalise(name: str) -> str:
    """The spelling `EnvSecrets` falls back to, so `felix-auth-api-keys` is `FELIX_AUTH_API_KEYS`."""
    return name.strip().upper().replace("-", "_")


_RESERVED = frozenset(_normalise(n) for n in hydrated_secret_names() | _INFRA_SECRET_NAMES)


def is_reserved_secret_name(name: str) -> bool:
    """Whether `name` is one of Felix's own secrets: a `FELIX_*` setting (or `felix/…` in a cloud
    backend), any candidate name in the hydrate map, or an infrastructure credential. Compared
    after the normalisation `EnvSecrets` applies, so no spelling of one escapes the check."""
    normalised = _normalise(name)
    return normalised.startswith(("FELIX_", "FELIX/")) or normalised in _RESERVED


@lru_cache(maxsize=4)
def _parse(raw: str) -> dict[str, frozenset[str] | None]:
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"not valid JSON ({exc.msg})") from None
    if not isinstance(parsed, dict):
        raise ValueError('must be a JSON object of secret name -> ["tenant", ...] or "*"')
    out: dict[str, frozenset[str] | None] = {}
    for name, tenants in parsed.items():
        if not name or name != name.strip() or any(ch.isspace() for ch in name):
            raise ValueError(f"secret name {name!r} must be non-empty with no whitespace")
        if is_reserved_secret_name(name):
            # Refused below whatever this says; listing it is a mistake worth a failed boot.
            raise ValueError(f"{name!r} is one of Felix's own secrets and cannot be opened to manifests")
        out[name] = parse_tenant_list(tenants, what=f"secret {name!r}")
    return out


def parse_manifest_secrets(settings: Any) -> dict[str, frozenset[str] | None]:
    """`FELIX_MANIFEST_SECRETS`: secret name -> the tenants whose manifests may reference it
    (`None` for `"*"`). `ValueError` naming what is wrong; boot validation turns that into a
    refusal, so a malformed value is never read as "nothing allowed" in silence."""
    return _parse(str(getattr(settings, "manifest_secrets", "") or ""))


# --- secrets operator configuration already binds ----------------------------------------------

# Every other `Settings` string is scanned, not a hand-kept list of registries: a value that is a
# `secret:NAME` ref, or JSON holding one, binds that secret to an operator purpose (an egress
# credential scoped to a host, a webhook's signing key, a trigger's, a channel's, a provider key),
# and a setting added next year is covered the day it lands. Two fields are read differently.
_NOT_REFS = {
    # The allowlist itself: its keys are the names being opened, not names bound elsewhere.
    "manifest_secrets": "the allowlist being checked",
}
_NAME_LISTS = {
    # Comma-separated backend names resolved for masking, not refs.
    "secret_names": "comma-separated secret names",
}
_MAX_DEPTH = 8


def _ref_names_in(value: object, *, where: str, _depth: int = 0) -> list[str]:
    """Every `secret:NAME` (or `{"secret": "NAME"}`) inside operator-supplied JSON."""
    if _depth > _MAX_DEPTH:
        # Refused rather than skipped: an unread level could hold the ref this scan exists to find.
        raise ValueError(f"{where} is nested deeper than {_MAX_DEPTH} levels; cannot check its secret refs")
    if isinstance(value, str):
        name = secret_ref_name(value)
        return [name] if name else []
    if isinstance(value, dict):
        if set(value) == {"secret"} and isinstance(value["secret"], str):
            name = secret_ref_name(value)
            return [name] if name else []
        return [n for v in value.values() for n in _ref_names_in(v, where=where, _depth=_depth + 1)]
    if isinstance(value, list):
        return [n for v in value for n in _ref_names_in(v, where=where, _depth=_depth + 1)]
    return []


def _names_in_setting(attr: str, value: str) -> list[str]:
    if attr in _NAME_LISTS:
        return [n.strip() for n in value.split(",") if n.strip()]
    text = value.strip()
    name = secret_ref_name(text)
    if name:
        return [name]
    if not text.startswith(("{", "[")):
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return []  # the setting's own validator refuses it
    return _ref_names_in(parsed, where=f"FELIX_{attr.upper()}")


def operator_bound_secret_names(settings: Any) -> dict[str, str]:
    """Normalised secret name -> the `FELIX_*` setting that already binds it.

    Read before `hydrate_secrets` rewrites `FELIX_MODEL_PROVIDER_OPTIONS` refs into values, which
    is when `validate_runtime` asks. `ValueError` for JSON nested past what is scanned."""
    bound: dict[str, str] = {}
    for attr in type(settings).model_fields:
        value = getattr(settings, attr, None)
        if attr in _NOT_REFS or not isinstance(value, str) or not value.strip():
            continue
        for name in _names_in_setting(attr, value):
            bound.setdefault(_normalise(name), f"FELIX_{attr.upper()}")
    return bound


def assert_manifest_secrets_unbound(settings: Any) -> None:
    """`ValueError` for an allowlisted name an operator setting already binds (boot check)."""
    allowed = parse_manifest_secrets(settings)
    if not allowed:
        return
    bound = operator_bound_secret_names(settings)
    for name in allowed:
        owner = bound.get(_normalise(name))
        if owner:
            raise ValueError(
                f"{name!r} is already bound by {owner}; give manifests a separate secret rather than "
                "sharing that one"
            )


# --- the per-request check ---------------------------------------------------------------------


def assert_manifest_secret_permitted(name: str, tenant_id: str, settings: Any) -> None:
    """Raise `SecretNotPermittedError` unless `tenant_id`'s manifests may reference `name`.

    Felix's own secrets are refused first and always. Anything else must be listed in
    `FELIX_MANIFEST_SECRETS` for this tenant, or for `"*"`: an allowlist, because every backend
    can see far more than a tenant should, and a denylist fails open on the next credential
    someone adds to the environment. Matched exactly, so `github-token` is not allowed by
    `GITHUB_TOKEN`; a spelling the operator did not write is refused, never widened.
    """
    shown = loggable(name, limit=120)
    if is_reserved_secret_name(name):
        raise SecretNotPermittedError(
            f"secret:{shown} is one of Felix's own credentials and may not be referenced from a "
            "manifest; FELIX_MANIFEST_SECRETS cannot allow it"
        )
    try:
        allowed = parse_manifest_secrets(settings)
    except ValueError:
        allowed = {}  # boot validation refuses this; fail closed if it got past
    if name not in allowed or not tenant_allowed(allowed[name], tenant_id):
        raise SecretNotPermittedError(
            f"secret:{shown} is not permitted for this tenant's manifests; the operator must list it "
            "in FELIX_MANIFEST_SECRETS"
        )


class TenantScopedSecrets:
    """A `SecretsProvider` that resolves only what one tenant's manifests may reference.

    Wraps the configured backend for every lookup whose name a manifest chose, so the check sits
    in front of `get` itself: no resolve path through it can reach the backend unchecked.
    """

    def __init__(self, inner: SecretsProvider, *, tenant_id: str, settings: Any) -> None:
        self._inner = inner
        self._tenant_id = tenant_id
        self._settings = settings

    async def get(self, name: str) -> str | None:
        assert_manifest_secret_permitted(name, self._tenant_id, self._settings)
        return await self._inner.get(name)


def tenant_secrets_provider(settings: object, tenant_id: str) -> SecretsProvider:
    """The secrets backend as a manifest compiled for `tenant_id` may use it (`TenantScopedSecrets`).
    Every resolve of a manifest-supplied `secret:NAME` goes through this, never `build_secrets`."""
    return TenantScopedSecrets(build_secrets(settings), tenant_id=tenant_id, settings=settings)


__all__ = [
    "TenantScopedSecrets",
    "assert_manifest_secret_permitted",
    "assert_manifest_secrets_unbound",
    "is_reserved_secret_name",
    "operator_bound_secret_names",
    "parse_manifest_secrets",
    "tenant_secrets_provider",
]
