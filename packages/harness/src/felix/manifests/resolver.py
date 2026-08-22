"""Tenant-aware manifest resolver with canary hash routing."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from felix.manifests.loader import load_bundled, parse_manifest
from felix.manifests.schema import Manifest, assert_valid_manifest_name

ManifestSource = Literal["tenant_postgres", "tenant_object", "global_object", "bundled"]
ManifestVariant = Literal["stable", "canary"]

ACTIVE_TTL_MS = 30_000


class ManifestStore(Protocol):
    async def get_active(self, tenant_id: str, name: str) -> ActivePointer | None: ...
    async def get_version(self, tenant_id: str, name: str, version: int) -> Manifest | None: ...


class ObjectStore(Protocol):
    async def get_json(self, key: str) -> Any | None: ...


@dataclass(slots=True)
class ActivePointer:
    version: int
    canary_version: int | None = None
    canary_weight: int = 0


@dataclass(slots=True)
class ResolvedManifest:
    manifest: Manifest
    source: ManifestSource
    version: int | None = None
    variant: ManifestVariant | None = None
    cache_key: str = ""


@dataclass
class ResolveOptions:
    pin_version: int | None = None
    thread_id: str | None = None


_version_blob_cache: dict[str, Manifest] = {}
_active_pointer_cache: dict[str, dict[str, Any]] = {}
_tenant_obj_cache: dict[str, Manifest] = {}
_global_obj_cache: dict[str, Manifest] = {}


def _blob_key(tenant_id: str, name: str, version: int) -> str:
    return f"{tenant_id}#{name}#{version}"


def _pointer_key(tenant_id: str, name: str) -> str:
    return f"{tenant_id}#{name}"


def pick_variant(
    *,
    tenant_id: str,
    thread_id: str,
    manifest_name: str,
    stable_version: int,
    canary_version: int,
    canary_weight: int,
) -> ManifestVariant:
    """Deterministic 0..99 bucket via SHA-256 of the routing tuple."""
    if canary_weight <= 0:
        return "stable"
    if canary_weight >= 100:
        return "canary"
    if not thread_id:
        return "stable"
    key = f"{tenant_id}|{thread_id}|{manifest_name}|{stable_version}|{canary_version}"
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    bucket = int.from_bytes(digest[:4], "big") % 100
    return "canary" if bucket < canary_weight else "stable"


async def _read_tenant_postgres(
    store: ManifestStore | None,
    tenant_id: str,
    name: str,
    opts: ResolveOptions,
) -> ResolvedManifest | None:
    if store is None:
        return None
    if opts.pin_version is not None:
        pointer = ActivePointer(version=opts.pin_version)
    else:
        cached = _active_pointer_cache.get(_pointer_key(tenant_id, name))
        now = time.time() * 1000
        if cached and cached["expires_at"] > now:
            pointer = ActivePointer(
                version=cached["version"],
                canary_version=cached["canary_version"],
                canary_weight=cached["canary_weight"],
            )
        else:
            active = await store.get_active(tenant_id, name)
            if active is None:
                return None
            pointer = active
            _active_pointer_cache[_pointer_key(tenant_id, name)] = {
                "version": pointer.version,
                "canary_version": pointer.canary_version,
                "canary_weight": pointer.canary_weight,
                "expires_at": now + ACTIVE_TTL_MS,
            }

    variant: ManifestVariant = "stable"
    resolved_version = pointer.version
    if opts.pin_version is None and pointer.canary_version is not None and pointer.canary_weight > 0:
        variant = pick_variant(
            tenant_id=tenant_id,
            thread_id=opts.thread_id or "",
            manifest_name=name,
            stable_version=pointer.version,
            canary_version=pointer.canary_version,
            canary_weight=pointer.canary_weight,
        )
        if variant == "canary":
            resolved_version = pointer.canary_version

    cached_blob = _version_blob_cache.get(_blob_key(tenant_id, name, resolved_version))
    if cached_blob is not None:
        return ResolvedManifest(
            manifest=cached_blob,
            source="tenant_postgres",
            version=resolved_version,
            variant=variant,
            cache_key=f"tenant_postgres:{tenant_id}#{name}#{resolved_version}",
        )
    row = await store.get_version(tenant_id, name, resolved_version)
    if row is None:
        return None
    _version_blob_cache[_blob_key(tenant_id, name, resolved_version)] = row
    return ResolvedManifest(
        manifest=row,
        source="tenant_postgres",
        version=resolved_version,
        variant=variant,
        cache_key=f"tenant_postgres:{tenant_id}#{name}#{resolved_version}",
    )


async def _read_object(
    store: ObjectStore | None,
    key: str,
    cache: dict[str, Manifest],
) -> Manifest | None:
    if store is None:
        return None
    if key in cache:
        return cache[key]
    raw = await store.get_json(key)
    if raw is None:
        return None
    if isinstance(raw, (bytes, str)):
        raw = json.loads(raw)
    parsed = parse_manifest(raw)
    cache[key] = parsed
    return parsed


async def resolve_manifest(
    tenant_id_or_settings: Any,
    name_or_tenant: str | None = None,
    name: str | None = None,
    *,
    opts: ResolveOptions | None = None,
    thread_id: str | None = None,
    pin_version: int | None = None,
    manifest_store: ManifestStore | None = None,
    object_store: ObjectStore | None = None,
    bundled_dir: str | None = None,
    settings: Any = None,
) -> ResolvedManifest:
    """Resolve: postgres active → tenant object → global object → bundled.

    Accepts either:
      resolve_manifest(tenant_id, name, ...)
      resolve_manifest(settings, tenant_id, name, thread_id=...)
    """
    from felix.config import Settings

    tenant_id: str
    manifest_name: str
    if isinstance(tenant_id_or_settings, Settings) or (
        settings is None
        and name_or_tenant is not None
        and name is not None
        and hasattr(tenant_id_or_settings, "database_url")
    ):
        # (settings, tenant_id, name)
        tenant_id = str(name_or_tenant)
        manifest_name = str(name)
        settings = tenant_id_or_settings
    elif name is not None and name_or_tenant is not None:
        tenant_id = str(name_or_tenant)
        manifest_name = str(name)
    elif name_or_tenant is not None and name is None:
        # (tenant_id, name)
        tenant_id = str(tenant_id_or_settings)
        manifest_name = str(name_or_tenant)
    else:
        raise TypeError("resolve_manifest(tenant_id, name) or resolve_manifest(settings, tenant_id, name)")

    assert_valid_manifest_name(manifest_name)
    options = opts or ResolveOptions(
        pin_version=pin_version,
        thread_id=thread_id,
    )
    if opts is None and (thread_id is not None or pin_version is not None):
        options = ResolveOptions(pin_version=pin_version, thread_id=thread_id)

    tenant_pg = await _read_tenant_postgres(manifest_store, tenant_id, manifest_name, options)
    if tenant_pg is not None:
        return tenant_pg

    if options.pin_version is not None:
        raise LookupError(f"Unknown manifest version: {manifest_name}@{options.pin_version}")

    tenant_obj = await _read_object(
        object_store,
        f"manifests/{tenant_id}/{manifest_name}.json",
        _tenant_obj_cache,
    )
    if tenant_obj is not None:
        return ResolvedManifest(
            manifest=tenant_obj,
            source="tenant_object",
            cache_key=f"tenant_object:{tenant_id}#{manifest_name}",
        )

    global_obj = await _read_object(
        object_store,
        f"manifests/{manifest_name}.json",
        _global_obj_cache,
    )
    if global_obj is not None:
        return ResolvedManifest(
            manifest=global_obj,
            source="global_object",
            cache_key=f"global_object:{manifest_name}",
        )

    try:
        bundled = load_bundled(manifest_name, bundled_dir=bundled_dir)
    except FileNotFoundError as exc:
        raise LookupError(f"Unknown manifest: {manifest_name}") from exc
    return ResolvedManifest(
        manifest=bundled,
        source="bundled",
        cache_key=f"bundled:{manifest_name}",
    )


def invalidate_active(tenant_id: str, name: str) -> None:
    _active_pointer_cache.pop(_pointer_key(tenant_id, name), None)


def clear_resolver_cache() -> None:
    _version_blob_cache.clear()
    _active_pointer_cache.clear()
    _tenant_obj_cache.clear()
    _global_obj_cache.clear()


__all__ = [
    "ActivePointer",
    "ManifestSource",
    "ManifestStore",
    "ManifestVariant",
    "ObjectStore",
    "ResolveOptions",
    "ResolvedManifest",
    "clear_resolver_cache",
    "invalidate_active",
    "pick_variant",
    "resolve_manifest",
]
