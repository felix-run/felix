"""Cloud-agnostic secrets — env/file first; AWS SM and GCP SM adapters."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Protocol, runtime_checkable

logger = logging.getLogger("felix.secrets")

# Values resolved via hydrate_secrets — used for output masking.
_resolved_secret_values: list[str] = []

# Settings attrs that may hold secrets, and candidate names in the secrets backend.
_HYDRATE_MAP: dict[str, tuple[str, ...]] = {
    "anthropic_api_key": (
        "ANTHROPIC_API_KEY",
        "anthropic_api_key",
        "felix-anthropic-api-key",
        "felix/anthropic_api_key",
    ),
    "openai_api_key": ("OPENAI_API_KEY", "openai_api_key", "felix/openai_api_key"),
    "consumer_shared_secret": (
        "CONSUMER_SHARED_SECRET",
        "consumer_shared_secret",
        "felix/consumer_shared_secret",
    ),
    "webhook_secret": ("WEBHOOK_SECRET", "webhook_secret", "felix/webhook_secret"),
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
        raw = Path(name)
        if raw.is_absolute() or ".." in raw.parts:
            logger.warning("file_secrets_reject path=%s", name)
            return None
        path = (self._root / raw).resolve()
        try:
            path.relative_to(self._root)
        except ValueError:
            logger.warning("file_secrets_reject path=%s", name)
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


def build_secrets(settings: object) -> SecretsProvider:
    from felix.config import Settings

    assert isinstance(settings, Settings)
    backend = getattr(settings, "secrets_backend", "env")
    if backend == "file":
        return FileSecrets(getattr(settings, "secrets_dir", "./secrets"))
    if backend == "aws":
        return AwsSecretsManager(getattr(settings, "aws_region", "us-east-1"))
    if backend == "gcp":
        project = getattr(settings, "gcp_project", "") or ""
        if not project:
            raise RuntimeError("FELIX_GCP_PROJECT required for secrets_backend=gcp")
        return GcpSecretManager(project)
    return EnvSecrets()


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
                logger.debug("secret_lookup_failed name=%s", name, exc_info=True)
                continue
            if val:
                setattr(settings, attr, val)
                if len(val) >= 8:
                    found.append(val)
                break

    extra = getattr(settings, "secret_names", "") or ""
    for name in (n.strip() for n in extra.split(",") if n.strip()):
        try:
            val = await provider.get(name)
        except Exception:
            logger.debug("secret_lookup_failed name=%s", name, exc_info=True)
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
    return out


__all__ = [
    "AwsSecretsManager",
    "EnvSecrets",
    "FileSecrets",
    "GcpSecretManager",
    "SecretsProvider",
    "build_secrets",
    "collected_secret_values",
    "hydrate_secrets",
]
