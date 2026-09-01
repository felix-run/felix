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
from felix.manifests.governance import GovernanceError, assert_stdio_allowed
from felix.manifests.loader import parse_manifest
from felix.manifests.schema import Manifest
from felix.session.store import validate_checkpointer_config
from pydantic import BaseModel, Field

from felix_api.threads import effective_thread_id

router = APIRouter(tags=["Manifests"])
# Mounted only when manifests are writable. Under `manifest_source=bundled` these are
# never registered, so the verbs are absent from the app and from the OpenAPI document
# rather than being present and refusing — and Starlette answers a PUT with a spec-correct
# 405 carrying `Allow: GET`.
write_router = APIRouter(tags=["Manifests"])


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
    settings = request.app.state.settings
    if settings.bundled_only:
        # This is the endpoint an operator checks after flipping the posture, so it must not
        # report Postgres rows the resolver will never serve.
        from felix.manifests.loader import list_bundled

        rows = [{"name": name, "version": None, "source": "bundled"} for name in list_bundled()]
        return {"items": rows, "manifests": rows}
    rows = [
        # Tagged for the same reason the bundled rows are: the two postures return different
        # shapes, and a client should not have to infer which one it got. Note this is the
        # *configured* view — version plus any canary — and is deliberately not the same
        # question as "what would my next request resolve to", which `GET /manifests/{name}`
        # answers.
        {**row, "source": "store"}
        for row in await manifest_store.list_active(settings, tenant_id_from_request(request))
    ]
    return {"items": rows, "manifests": rows}


@router.get("/{name}")
async def get_manifest(
    name: str,
    request: Request,
    version: int | None = None,
    thread_id: str | None = None,
) -> Any:
    """Resolve a manifest as a request would see it.

    ``thread_id`` is the same suffix the chat routes take. Canary assignment is a
    deterministic hash over (tenant, thread, manifest, both versions), so without
    a thread there is nothing to hash and ``variant`` is always ``stable`` — pass
    the thread to learn which side actually serves it.
    """
    from felix.manifests import store as manifest_store
    from felix.runtime import resolve_tenant_manifest

    require_mgmt_scopes(request, SCOPE_MANIFESTS_READ)
    settings = request.app.state.settings
    tenant = tenant_id_from_request(request)
    if version is not None:
        if settings.bundled_only:
            # A version names a stored revision, and under this posture there are none —
            # returning one would hand back a manifest the resolver is built to refuse.
            raise HTTPException(status_code=404, detail="not_found")
        row = await manifest_store.get_version(settings, tenant, name, version)
        if row is None:
            raise HTTPException(status_code=404, detail="not_found")
        return row
    thread = effective_thread_id(tenant, thread_id)
    if thread_id and thread is None:
        raise HTTPException(status_code=400, detail="invalid_thread_id")
    resolved = await resolve_tenant_manifest(settings, tenant, name, thread_id=thread)
    return {
        "name": name,
        "version": resolved.version,
        "variant": resolved.variant or "stable",
        "manifest": resolved.manifest.model_dump(mode="json"),
    }


@write_router.put("/{name}")
async def upsert_manifest(name: str, body: ManifestUpsert, request: Request) -> Any:
    from felix.manifests import store as manifest_store

    require_mgmt_scopes(request, SCOPE_MANIFESTS_WRITE)
    parsed: Manifest = parse_manifest(body.manifest)
    if parsed.metadata.name != name:
        raise HTTPException(status_code=400, detail="name_mismatch")
    # Refuse at write time: compiling this manifest would spawn the command.
    try:
        assert_stdio_allowed(parsed, request.app.state.settings)
    except GovernanceError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    # Same reason. `memory.checkpointer` is an open string resolved against a
    # registry, so pydantic no longer catches a typo — and stored, it would raise
    # inside every build, i.e. a 500 on every request for this manifest until
    # someone read a traceback. A 400 here is the same trade `assert_stdio_allowed`
    # makes above.
    try:
        validate_checkpointer_config(
            parsed.spec.memory.checkpointer,
            session_strategy=parsed.spec.session.strategy,
            compact_after_turn=parsed.spec.session.compact_after_turn,
            memory_capture=parsed.spec.memory.capture.enabled,
            memory_recall_tools=parsed.spec.memory.recall.tools,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    row = await manifest_store.put_version(
        request.app.state.settings,
        tenant_id_from_request(request),
        name,
        parsed,
        created_by=subject_from_request(request),
        comment=body.comment,
    )
    return row


@write_router.post("/{name}/canary")
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


@write_router.post("/{name}/rollback")
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


@write_router.delete("/{name}/canary")
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
