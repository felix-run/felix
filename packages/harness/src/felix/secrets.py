"""Cloud-agnostic secrets — env/file first; AWS SM and GCP SM adapters."""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from felix.config import Settings

from felix.logging_setup import loggable

logger = logging.getLogger("felix.secrets")

# Values resolved via hydrate_secrets — used for output masking.
_resolved_secret_values: list[str] = []

# Manifest auth/env may use ``secret:NAME`` (or ``{"secret": "NAME"}``).
_SECRET_REF_RE = re.compile(r"^secret:(.+)$", re.IGNORECASE)
# Heuristic: Bearer/Basic tokens or long hex/base64-looking blobs.
# Credential shapes: an HTTP auth scheme with a token, a long opaque blob, a vendor-prefixed
# key (`sk-…`, `AKIA…`), a JWT, or `user:password`. A heuristic, used where a false positive
# is a refusal the author can answer with `secret:NAME`; the read side redacts any literal.
_PLAINTEXT_AUTH_RE = re.compile(
    r"^(?:(?:bearer|basic|token|apikey|api-key)\s+\S+"
    r"|[A-Za-z0-9+/_-]{24,}={0,2}"
    r"|(?:sk|rk|pk|xox[abp]|ghp|gho|glpat)[-_][A-Za-z0-9_-]{8,}"
    r"|AKIA[A-Z0-9]{12,}"
    r"|[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})$",
    re.IGNORECASE,
)
# `user:password` — only when the right-hand side is password-shaped (letters and digits,
# eight or more), so `HOST:localhost` and `format:pretty` stay ordinary settings.
_USERPASS_RE = re.compile(r"^[^\s:@/]{1,64}:(?=[^\s@/]*\d)(?=[^\s@/]*[A-Za-z])[^\s@/]{8,}$")


# The model-provider half of the map below is derived from the provider descriptors rather
# than listed by hand.
# This map is also what feeds `collected_secret_values()`, so a provider missing from it is
# not merely un-hydrated — its key is never masked out of tool output. Deriving it means
# adding a provider cannot silently create that hole.
def _provider_secret_entries() -> dict[str, tuple[str, ...]]:
    from felix_ai.providers import builtin_provider_specs

    return {
        spec.api_key_config_key: spec.secret_names
        for spec in builtin_provider_specs()
        if spec.api_key_config_key and spec.secret_names
    }


# Settings attrs that may hold secrets, and candidate names in the secrets backend.
_HYDRATE_MAP: dict[str, tuple[str, ...]] = {
    **_provider_secret_entries(),
    "consumer_shared_secret": (
        "CONSUMER_SHARED_SECRET",
        "consumer_shared_secret",
        "felix/consumer_shared_secret",
    ),
    "webhook_secret": ("WEBHOOK_SECRET", "webhook_secret", "felix/webhook_secret"),
    # A search backend's credential is a credential like any other. Left out of this map it
    # was the one key an operator on AWS/GCP Secrets Manager had to supply as plaintext env,
    # and it reached none of the four redaction sinks. The replacement cost noted in
    # `collected_secret_values` is negligible for an API key: high entropy, so it never
    # matches unrelated text.
    "search_api_key": ("SEARCH_API_KEY", "search_api_key", "felix/search_api_key"),
}


@runtime_checkable
class SecretsProvider(Protocol):
    async def get(self, name: str) -> str | None: ...


class EnvSecrets:
    async def get(self, name: str) -> str | None:
        return os.environ.get(name) or os.environ.get(name.upper().replace("-", "_"))


class FileSecrets:
    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).resolve()

    async def get(self, name: str) -> str | None:
        # Reject path traversal; only basename-like relative keys under root.
        #
        # The name is logged through `loggable` because on this path it is, by
        # construction, a value someone chose and the loader refused -- so a newline in
        # it would forge a log entry, and the entry it would forge sits right beside
        # genuine "rejected" records. The name itself is safe to log and worth logging:
        # it is the key, never the secret, and a rejected lookup is only actionable if
        # you can see what was asked for.
        raw = Path(name)
        if raw.is_absolute() or ".." in raw.parts:
            logger.warning("file_secrets_reject path=%s", loggable(name, limit=120))
            return None
        path = (self._root / raw).resolve()
        try:
            path.relative_to(self._root)
        except ValueError:
            logger.warning("file_secrets_reject path=%s", loggable(name, limit=120))
            return None
        if path.is_file():
            return path.read_text(encoding="utf-8").rstrip("\n")
        return None


class AwsSecretsManager:
    def __init__(self, region: str = "us-east-1") -> None:
        self._region = region

    async def get(self, name: str) -> str | None:
        try:
            import boto3
        except ImportError as e:
            raise RuntimeError("AWS secrets require: uv sync --extra aws") from e
        client = boto3.client("secretsmanager", region_name=self._region)
        try:
            resp = client.get_secret_value(SecretId=name)
        except client.exceptions.ResourceNotFoundException:
            return None
        if "SecretString" in resp:
            val = resp["SecretString"]
            try:
                parsed = json.loads(val)
                if isinstance(parsed, dict) and "value" in parsed:
                    return str(parsed["value"])
            except json.JSONDecodeError:
                pass
            return val
        return None


class GcpSecretManager:
    def __init__(self, project_id: str) -> None:
        self._project = project_id

    async def get(self, name: str) -> str | None:
        try:
            from google.cloud import secretmanager
        except ImportError as e:
            raise RuntimeError("GCP secrets require: uv sync --extra gcp") from e
        client = secretmanager.SecretManagerServiceClient()
        path = f"projects/{self._project}/secrets/{name}/versions/latest"
        try:
            resp = client.access_secret_version(request={"name": path})
        except Exception:
            return None
        return resp.payload.data.decode("utf-8")


SecretsFactory = Callable[["Settings"], SecretsProvider]

_backends: dict[str, SecretsFactory] = {}


def register_secrets_backend(name: str, factory: SecretsFactory) -> None:
    """Register a secrets backend for ``FELIX_SECRETS_BACKEND=<name>``.

    ``SecretsProvider`` was already a Protocol behind a hardcoded if/elif. Call
    this at import time from a ``felix.plugins`` entry point to add Vault,
    1Password, SOPS, or a bespoke provider.
    """
    _backends[name] = factory


def list_secrets_backends() -> list[str]:
    return sorted(_backends)


def _build_env(settings: Any) -> SecretsProvider:
    _ = settings
    return EnvSecrets()


def _build_file(settings: Any) -> SecretsProvider:
    return FileSecrets(getattr(settings, "secrets_dir", "./secrets"))


def _build_aws(settings: Any) -> SecretsProvider:
    return AwsSecretsManager(getattr(settings, "aws_region", "us-east-1"))


def _build_gcp(settings: Any) -> SecretsProvider:
    project = getattr(settings, "gcp_project", "") or ""
    if not project:
        raise RuntimeError("FELIX_GCP_PROJECT required for secrets_backend=gcp")
    return GcpSecretManager(project)


register_secrets_backend("env", _build_env)
register_secrets_backend("file", _build_file)
register_secrets_backend("aws", _build_aws)
register_secrets_backend("gcp", _build_gcp)


def build_secrets(settings: object) -> SecretsProvider:
    """Factory from FELIX_SECRETS_BACKEND (default env). Backends are registrable.

    An unknown backend raises: falling back to `env` would silently serve empty
    secrets and fail much later, somewhere unrelated.
    """
    from felix.config import Settings

    assert isinstance(settings, Settings)
    backend = getattr(settings, "secrets_backend", "env")
    factory = _backends.get(backend)
    if factory is None:
        raise RuntimeError(
            f"Unknown FELIX_SECRETS_BACKEND={backend!r} (registered: {', '.join(list_secrets_backends())})"
        )
    return factory(settings)


async def _hydrate_provider_options(settings: Any, provider: SecretsProvider) -> list[str]:
    """Resolve `secret:NAME` values inside `FELIX_MODEL_PROVIDER_OPTIONS`, in place.

    The hosted providers have no `Settings` field, so there is no attribute for the loop
    above to hydrate into — which is why their descriptors declare no `secret_names`. Their
    credential arrives through the options map instead, and a `secret:NAME` value there is
    resolved through the same backend, the same way MCP and peer refs already work:

        FELIX_MODEL_PROVIDER_OPTIONS={"groq": {"api_key": "secret:GROQ_API_KEY"}}

    Rewriting the setting in place keeps `resolve_provider_config` synchronous — it runs per
    agent build, and secret lookups are not something to do on that path.
    """
    raw = (getattr(settings, "model_provider_options", "") or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, dict):
        return []

    found: list[str] = []
    changed = False
    for opts in parsed.values():
        if not isinstance(opts, dict):
            continue
        for name, value in list(opts.items()):
            # Both spellings, since `normalize_secret_ref` accepts both everywhere else:
            # the object form was stringified into `"{'secret': 'NAME'}"` and sent as a
            # bearer token.
            if secret_ref_name(value) is None:
                continue
            try:
                resolved = await resolve_secret_value(provider, value, register=False)
            except Exception:
                # Drop it rather than leave the reference in place. `resolve_provider_config`
                # would otherwise read `"secret:GROQ_API_KEY"` as the api_key and ship the
                # internal secret *name* to the third-party endpoint and into any log of
                # that request. Removing it lets the settings-field fallback apply, or the
                # missing-credential path fire.
                logger.warning(
                    "provider option %s could not be resolved from the secrets backend; "
                    "dropping it rather than sending the reference upstream",
                    loggable(name, limit=60),
                )
                opts.pop(name, None)
                changed = True
                continue
            if resolved:
                opts[name] = resolved
                changed = True
    if changed:
        settings.model_provider_options = json.dumps(parsed)
    # Register every credential in the blob, not just the refs resolved above — and after
    # the rewrite, so a resolved ref is registered as its *value* rather than skipped as a
    # reference. Audit rows, session events and fiber state redact through
    # `collected_secret_values()` with no settings, so they only ever see this process-global
    # list: a literal `{"groq": {"api_key": "gsk_..."}}`, the form `.env.example` documents,
    # was masked in tool output and nowhere else. `deploy/GOVERNANCE.md` promises all four.
    found.extend(_provider_option_secrets(settings))
    return found


async def hydrate_secrets(settings: object) -> list[str]:
    """Fill empty settings attrs from FELIX_SECRETS_BACKEND; cache values for masking.

    Also resolves comma-separated ``FELIX_SECRET_NAMES`` for redaction only.
    """
    global _resolved_secret_values
    from felix.config import Settings

    assert isinstance(settings, Settings)
    provider = build_secrets(settings)
    found: list[str] = []

    for attr, candidates in _HYDRATE_MAP.items():
        current = getattr(settings, attr, "") or ""
        if current:
            if len(current) >= 8:
                found.append(current)
            continue
        for name in candidates:
            try:
                val = await provider.get(name)
            except Exception:
                logger.debug("secret_lookup_failed name=%s", loggable(name, limit=120), exc_info=True)
                continue
            if val:
                setattr(settings, attr, val)
                if len(val) >= 8:
                    found.append(val)
                break

    found.extend(await _hydrate_provider_options(settings, provider))

    extra = getattr(settings, "secret_names", "") or ""
    for name in (n.strip() for n in extra.split(",") if n.strip()):
        try:
            val = await provider.get(name)
        except Exception:
            logger.debug("secret_lookup_failed name=%s", loggable(name, limit=120), exc_info=True)
            continue
        if val and len(val) >= 8:
            found.append(val)

    # Dedupe while preserving order
    seen: set[str] = set()
    ordered: list[str] = []
    for v in found:
        if v not in seen:
            seen.add(v)
            ordered.append(v)
    _resolved_secret_values = ordered
    return ordered


def collected_secret_values(settings: object | None = None) -> list[str]:
    """Secret strings to redact from tool output (hydrated + settings attrs)."""
    out = list(_resolved_secret_values)
    if settings is not None:
        for attr in _HYDRATE_MAP:
            val = getattr(settings, attr, "") or ""
            if val and len(val) >= 8 and val not in out:
                out.append(val)
        for val in _provider_option_secrets(settings):
            if val not in out:
                out.append(val)
    return out


def _leaf_strings(value: object, *, _depth: int = 0) -> list[str]:
    """Every string ≥8 chars inside a nested option value.

    Bounded depth because this walks operator-supplied JSON; a cycle is impossible through
    `json.loads`, but a deeply nested blob should not cost unbounded recursion.
    """
    if _depth > 6:
        return []
    if isinstance(value, str):
        return [value] if len(value) >= 8 else []
    if isinstance(value, dict):
        return [s for v in value.values() for s in _leaf_strings(v, _depth=_depth + 1)]
    if isinstance(value, list):
        return [s for v in value for s in _leaf_strings(v, _depth=_depth + 1)]
    return []


def _provider_option_secrets(settings: object) -> list[str]:
    """Credentials configured through `FELIX_MODEL_PROVIDER_OPTIONS`.

    A key supplied there is not a `Settings` attribute, so the hydrate loop cannot see it —
    and a provider credential that never reaches this list is never masked out of tool
    output.

    Which values are secret is decided by allowlisting the option names that are
    *addressing*, per provider, from that provider's own descriptor. The previous version
    matched names containing key/token/secret/password, which is a denylist and failed open
    for exactly the third-party providers this options map exists to serve: a credential
    option called `credential`, `authorization` or `bearer` was published verbatim.
    `_TRUSTED_TRANSPORTS` records the same reasoning for tool transports.

    Erring toward masking is deliberate, but it is not free: `redact_text` is an
    unconditional string replacement over session events, audit payloads and fiber state, so
    a long low-entropy value wrongly treated as secret rewrites unrelated text everywhere it
    appears. That is the cost being traded against leaking a credential, and it is why the
    exemption is derived from what each provider actually consumes rather than guessed at.
    """
    raw = (getattr(settings, "model_provider_options", "") or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, dict):
        return []

    from felix_ai.providers import (
        CREDENTIAL_OPTION_NAMES,
        builtin_provider_specs,
        placeholder_names,
    )

    specs = {spec.name: spec for spec in builtin_provider_specs()}
    found: list[str] = []
    for provider_name, opts in parsed.items():
        if not isinstance(opts, dict):
            continue
        configured = opts.get("base_url") if isinstance(opts.get("base_url"), str) else None
        spec = specs.get(str(provider_name))
        if spec is not None:
            # Per provider, not a union across all of them: `account_id` is addressing for
            # Cloudflare and means nothing to Groq, and exempting it everywhere would be the
            # same name-based over-reach this function exists to remove.
            addressing = spec.addressing_option_names(configured)
        else:
            # A plugin registers a bare factory, not a descriptor, so there is nothing to
            # ask. What it templates into its own endpoint is still derivable from it.
            addressing = (frozenset({"base_url"}) | placeholder_names(configured)) - CREDENTIAL_OPTION_NAMES
        for name, value in opts.items():
            # Mask the coerced form, because that is what actually goes on the wire:
            # `parse_provider_options` does `str(v)` on every value, so an integer or a
            # nested dict becomes a live credential while `isinstance(value, str)` skipped
            # it here. The masker and the parser have to agree on what a value *is*.
            text = value if isinstance(value, str) else str(value)
            # Below 8 characters a redaction does more harm than good — it would rewrite
            # incidental matches throughout tool output. `hydrate_secrets` uses the same floor.
            if len(text) < 8:
                continue
            if name in addressing:
                continue
            # An unresolved `secret:NAME` is a reference, not a credential. Masking it
            # redacts the one diagnostic that names what failed to resolve, out of exactly
            # the logs someone is reading to find out why.
            # `value`, not `text`: the object form `{"secret": "NAME"}` coerces to
            # `"{'secret': 'NAME'}"`, which `secret_ref_name` does not recognise, so the
            # unresolved-ref diagnostic was being masked after all.
            if secret_ref_name(value) is not None:
                continue
            found.append(text)
            # A nested value is registered as its Python repr, which is single-quoted.
            # Re-render the same data as JSON, or pull out the leaf, and `redact_text` no
            # longer matches — so register the leaves too.
            found.extend(_leaf_strings(value))
    return found


def register_resolved_secret(value: str) -> None:
    """Add a runtime-resolved secret to the masking list (deduped)."""
    global _resolved_secret_values
    if not value or len(value) < 8:
        return
    if value in _resolved_secret_values:
        return
    _resolved_secret_values = [*_resolved_secret_values, value]


def normalize_secret_ref(value: str | dict[str, Any] | None) -> str:
    """Normalize ``secret:NAME`` or ``{"secret": "NAME"}`` to a string form."""
    if value is None:
        return ""
    if isinstance(value, dict):
        name = value.get("secret")
        if name is None or not str(name).strip():
            raise ValueError("secret ref object requires non-empty 'secret' key")
        return f"secret:{str(name).strip()}"
    return str(value)


def secret_ref_name(value: str | dict[str, Any] | None) -> str | None:
    """Return the backend secret name if ``value`` is a secret ref, else None."""
    try:
        normalized = normalize_secret_ref(value)
    except ValueError:
        return None
    if not normalized:
        return None
    m = _SECRET_REF_RE.match(normalized.strip())
    if not m:
        return None
    name = m.group(1).strip()
    return name or None


def is_secret_ref(value: str | dict[str, Any] | None) -> bool:
    return secret_ref_name(value) is not None


def looks_like_plaintext_secret(value: str | None) -> bool:
    """True when a non-ref auth/env string looks like an embedded credential."""
    if not value or not value.strip():
        return False
    if is_secret_ref(value):
        return False
    text = value.strip()
    return bool(_PLAINTEXT_AUTH_RE.match(text) or _USERPASS_RE.match(text))


async def resolve_secret_value(
    provider: SecretsProvider,
    value: str | dict[str, Any] | None,
    *,
    register: bool = True,
) -> str:
    """Resolve a secret ref via ``provider``; pass through literal non-ref strings."""
    if value is None:
        return ""
    name = secret_ref_name(value)
    if name is None:
        return normalize_secret_ref(value) if isinstance(value, dict) else str(value)
    resolved = await provider.get(name)
    if resolved is None:
        raise ValueError(f"secret not found: {name}")
    if register:
        register_resolved_secret(resolved)
    return resolved


def redact_text(text: str, secrets: list[str] | None = None) -> str:
    """Replace known secret substrings with ``[REDACTED]``."""
    if not text:
        return text
    vals = secrets if secrets is not None else collected_secret_values()
    out = text
    for s in vals:
        if s and s in out:
            out = out.replace(s, "[REDACTED]")
    return out


def redact_json(obj: Any, secrets: list[str] | None = None) -> Any:
    """Recursively redact secret substrings in JSON-compatible structures."""
    vals = secrets if secrets is not None else collected_secret_values()
    if not vals:
        return obj
    if isinstance(obj, str):
        return redact_text(obj, vals)
    if isinstance(obj, dict):
        return {k: redact_json(v, vals) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_json(v, vals) for v in obj]
    return obj


__all__ = [
    "AwsSecretsManager",
    "EnvSecrets",
    "FileSecrets",
    "GcpSecretManager",
    "SecretsFactory",
    "SecretsProvider",
    "build_secrets",
    "collected_secret_values",
    "hydrate_secrets",
    "is_secret_ref",
    "list_secrets_backends",
    "looks_like_plaintext_secret",
    "normalize_secret_ref",
    "redact_json",
    "redact_text",
    "register_resolved_secret",
    "register_secrets_backend",
    "resolve_secret_value",
    "secret_ref_name",
]
