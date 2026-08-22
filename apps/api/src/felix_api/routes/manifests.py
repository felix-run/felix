"""Manifest CRUD, canary, and rollback."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from felix.auth.mgmt import (
    SCOPE_MANIFESTS_READ,
    SCOPE_MANIFESTS_WRITE,
    require_mgmt_scopes,
    subject_from_request,
    tenant_id_from_request,
)
from felix.manifests.loader import parse_manifest
from felix.manifests.schema import Manifest
from pydantic import BaseModel, Field

router = APIRouter(tags=["Manifests"])


class ManifestUpsert(BaseModel):
    model_config = {"extra": "forbid"}

    manifest: dict[str, Any]
    comment: str = ""


class CanaryRequest(BaseModel):
    model_config = {"extra": "forbid"}

    canary_version: int
    canary_weight: int = Field(ge=0, le=100)


class RollbackRequest(BaseModel):
    model_config = {"extra": "forbid"}

    version: int
    comment: str = "rollback"


@router.get("")
@router.get("/")
async def list_manifests(request: Request) -> dict[str, Any]:
    from felix.manifests import store as manifest_store

    require_mgmt_scopes(request, SCOPE_MANIFESTS_READ)
    rows = await manifest_store.list_active(request.app.state.settings, tenant_id_from_request(request))
    return {"items": rows, "manifests": rows}


@router.get("/{name}")
async def get_manifest(name: str, request: Request, version: int | None = None) -> Any:
    from felix.manifests import store as manifest_store
    from felix.runtime import resolve_tenant_manifest

    require_mgmt_scopes(request, SCOPE_MANIFESTS_READ)
    settings = request.app.state.settings
    tenant = tenant_id_from_request(request)
    if version is not None:
        row = await manifest_store.get_version(settings, tenant, name, version)
        if row is None:
            raise HTTPException(status_code=404, detail="not_found")
        return row
    resolved = await resolve_tenant_manifest(settings, tenant, name)
    return {
        "name": name,
        "version": resolved.version,
        "variant": resolved.variant or "stable",
        "manifest": resolved.manifest.model_dump(mode="json"),
    }


@router.put("/{name}")
async def upsert_manifest(name: str, body: ManifestUpsert, request: Request) -> Any:
    from felix.manifests import store as manifest_store

    require_mgmt_scopes(request, SCOPE_MANIFESTS_WRITE)
    parsed: Manifest = parse_manifest(body.manifest)
    if parsed.metadata.name != name:
        raise HTTPException(status_code=400, detail="name_mismatch")
    row = await manifest_store.put_version(
        request.app.state.settings,
        tenant_id_from_request(request),
        name,
        parsed,
        created_by=subject_from_request(request),
        comment=body.comment,
    )
    return row


@router.post("/{name}/canary")
async def set_canary(name: str, body: CanaryRequest, request: Request) -> Any:
    from felix.manifests import store as manifest_store

    require_mgmt_scopes(request, SCOPE_MANIFESTS_WRITE)
    try:
        row = await manifest_store.set_canary(
            request.app.state.settings,
            tenant_id_from_request(request),
            name,
            canary_version=body.canary_version,
            canary_weight=body.canary_weight,
            updated_by=subject_from_request(request),
        )
    except LookupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if row is None:
        raise HTTPException(status_code=404, detail="not_found")
    return row


@router.post("/{name}/rollback")
async def rollback_manifest(name: str, body: RollbackRequest, request: Request) -> Any:
    from felix.manifests import store as manifest_store

    require_mgmt_scopes(request, SCOPE_MANIFESTS_WRITE)
    row = await manifest_store.activate_version(
        request.app.state.settings,
        tenant_id_from_request(request),
        name,
        version=body.version,
        updated_by=subject_from_request(request),
        comment=body.comment,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="not_found")
    return row


@router.delete("/{name}/canary")
async def clear_canary(name: str, request: Request) -> Any:
    from felix.manifests import store as manifest_store

    require_mgmt_scopes(request, SCOPE_MANIFESTS_WRITE)
    row = await manifest_store.set_canary(
        request.app.state.settings,
        tenant_id_from_request(request),
        name,
        canary_version=None,
        canary_weight=0,
        updated_by=subject_from_request(request),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="not_found")
    return row
