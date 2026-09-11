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


REDACTED = "[REDACTED]"

# The manifest sections that carry an outbound credential (`auth`, `env`) or a URL that
# could. Both the write-time check and the read-side redaction walk exactly these.
OUTBOUND_SECTIONS = ("mcp", "peers", "containers")
_URL_FIELDS = ("url", "gateway_url")


def url_userinfo(value: Any) -> bool:
    """Whether a URL carries `user:password@` — a credential the auth check never saw."""
    if not isinstance(value, str) or "@" not in value:
        return False
    from urllib.parse import urlsplit

    try:
        return bool(urlsplit(value).username or urlsplit(value).password)
    except ValueError:
        return True  # unparseable with an `@` in it: refuse rather than guess


def _strip_userinfo(value: str) -> str:
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(value)
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))


def redact_manifest_secrets(doc: dict[str, Any]) -> dict[str, Any]:
    """A manifest document safe to return to a reader.

    `secret:NAME` refs stay — they are the documented, non-secret form. Any other
    non-empty `auth`, any non-ref `env` value, and any URL userinfo is replaced: a literal
    there has no legitimate read-side value, and a heuristic that misses one credential
    shape hands it to `manifests:read`. Writes refuse these now, but a manifest stored
    before that, or written through the store directly, must not leak on read.
    """
    spec = doc.get("spec")
    if not isinstance(spec, dict):
        return doc
    out = {**doc, "spec": {**spec}}
    for section in OUTBOUND_SECTIONS:
        refs = spec.get(section)
        if not isinstance(refs, list):
            continue
        cleaned = []
        for ref in refs:
            if not isinstance(ref, dict):
                cleaned.append(ref)
                continue
            ref = dict(ref)
            auth = ref.get("auth")
            if isinstance(auth, str) and auth and not is_secret_ref(auth):
                ref["auth"] = REDACTED
            env = ref.get("env")
            if isinstance(env, dict):
                ref["env"] = {k: (v if not v or is_secret_ref(v) else REDACTED) for k, v in env.items()}
            for field in _URL_FIELDS:
                if url_userinfo(ref.get(field)):
                    ref[field] = _strip_userinfo(ref[field])
            cleaned.append(ref)
        out["spec"][section] = cleaned
    return out


def assert_no_plaintext_secrets(manifest: Manifest, *, strict: bool = True) -> None:
    """Reject an embedded credential on an outbound ref.

    Always: an `auth` that looks like a credential, an `env` value that does, and a URL
    carrying `user:password@`. Under ``strict`` — the framework / production posture —
    also any non-ref `env` value at all, because there a stdio server's environment is
    a place a secret ends up by accident and `secret:NAME` costs nothing.
    """
    errors: list[str] = []
    labels = {"mcp": "mcp_servers", "peers": "peers", "containers": "containers"}
    for section in OUTBOUND_SECTIONS:
        for ref in getattr(manifest.spec, section):
            label = f"{labels[section]}.{ref.name}"
            if looks_like_plaintext_secret(ref.auth):
                errors.append(f"{label}.auth looks like a plaintext secret")
            for field in _URL_FIELDS:
                if url_userinfo(getattr(ref, field, None)):
                    errors.append(f"{label}.{field} carries credentials in the URL; use auth: secret:NAME")
            for key, val in (getattr(ref, "env", None) or {}).items():
                if not val or is_secret_ref(val):
                    continue
                if strict:
                    errors.append(
                        f"{label}.env.{key} must use secret:NAME (plaintext env values are forbidden)"
                    )
                elif looks_like_plaintext_secret(val):
                    errors.append(f"{label}.env.{key} looks like a plaintext secret; use secret:NAME")
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
    "REDACTED",
    "PlaintextSecretError",
    "assert_no_plaintext_secrets",
    "redact_manifest_secrets",
    "resolve_container_ref",
    "resolve_mcp_ref",
    "resolve_outbound_secrets",
    "resolve_peer_ref",
]
