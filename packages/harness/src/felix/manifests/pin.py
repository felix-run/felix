"""Manifest compile pin — content hash + drift checks for pin_compile."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from felix.manifests.schema import Manifest


class ManifestDriftError(ValueError):
    """Pinned compile hash no longer matches the active manifest."""


def manifest_content_hash(manifest: Manifest) -> str:
    """Stable SHA-256 of the canonical manifest JSON (refs, not resolved secrets)."""
    payload = manifest.model_dump(mode="json", by_alias=True)
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def pin_fields(
    manifest: Manifest,
    *,
    version: int | None = None,
) -> dict[str, Any]:
    return {
        "manifest_name": manifest.metadata.name,
        "manifest_version": version,
        "manifest_hash": manifest_content_hash(manifest),
        "pin_compile": bool(manifest.spec.governance.pin_compile),
    }


def assert_pin_matches(
    pinned: dict[str, Any] | None,
    manifest: Manifest,
    *,
    version: int | None = None,
) -> None:
    """Refuse continue/resume when ``pin_compile`` and the hash drifted."""
    if not pinned:
        return
    if not pinned.get("pin_compile") and not manifest.spec.governance.pin_compile:
        return
    expected = pinned.get("manifest_hash")
    if not expected:
        return
    actual = manifest_content_hash(manifest)
    if expected != actual:
        raise ManifestDriftError(
            f"manifest hash drift for {manifest.metadata.name}: "
            f"pinned={expected[:12]}… current={actual[:12]}…"
        )
    pinned_version = pinned.get("manifest_version")
    if (
        pinned_version is not None
        and version is not None
        and int(pinned_version) != int(version)
    ):
        raise ManifestDriftError(
            f"manifest version drift for {manifest.metadata.name}: "
            f"pinned={pinned_version} current={version}"
        )


async def ensure_thread_pin(
    *,
    settings: Any,
    tenant_id: str,
    thread_id: str | None,
    manifest: Manifest,
    version: int | None = None,
) -> dict[str, Any]:
    """Check drift against prior pin; store pin when ``pin_compile`` is enabled."""
    from felix.session.thread_state import get_thread_meta, update_thread_meta

    fields = pin_fields(manifest, version=version)
    if not thread_id:
        return fields

    meta = await get_thread_meta(
        settings=settings, tenant_id=tenant_id, thread_id=thread_id
    )
    pinned = {
        "manifest_name": meta.get("manifest_name"),
        "manifest_version": meta.get("manifest_version"),
        "manifest_hash": meta.get("manifest_hash"),
        "pin_compile": meta.get("pin_compile"),
    }
    if pinned.get("manifest_hash"):
        assert_pin_matches(pinned, manifest, version=version)

    if fields["pin_compile"] or not pinned.get("manifest_hash"):
        # Always record hash on first touch; enforce only when pin_compile.
        if fields["pin_compile"]:
            await update_thread_meta(
                settings=settings,
                tenant_id=tenant_id,
                thread_id=thread_id,
                **fields,
            )
        elif not pinned.get("manifest_hash"):
            # Soft record without enforcement for observability.
            await update_thread_meta(
                settings=settings,
                tenant_id=tenant_id,
                thread_id=thread_id,
                manifest_name=fields["manifest_name"],
                manifest_version=fields["manifest_version"],
                manifest_hash=fields["manifest_hash"],
                pin_compile=False,
            )
    return fields


__all__ = [
    "ManifestDriftError",
    "assert_pin_matches",
    "ensure_thread_pin",
    "manifest_content_hash",
    "pin_fields",
]
