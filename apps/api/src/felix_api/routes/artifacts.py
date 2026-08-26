"""Read back a tool output that was too large to keep in the transcript.

`felix.artifacts` spills an oversized result to the object store and hands the model
a preview plus an `[artifact:<id> key=… chars=N]` marker. Nothing read it back:
`object_store.get` was called nowhere in the harness, so the full text existed and
was unreachable from every interface at once.

The manifest is a path segment and the tenant is not. That asymmetry is the point —
the tenant comes from the caller's own credentials, so no spelling of the reference
reaches another tenant's data, while the manifest is needed to locate the object and
is validated rather than trusted.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from felix.auth.mgmt import (
    SCOPE_ARTIFACTS_READ,
    require_mgmt_scopes,
    tenant_id_from_request,
)

router = APIRouter(tags=["Artifacts"])


@router.get("/{manifest_id}/{artifact_id}")
async def get_artifact(manifest_id: str, artifact_id: str, request: Request) -> dict[str, Any]:
    from felix.artifacts import read_artifact, valid_artifact_ref
    from felix.storage import get_object_store

    require_mgmt_scopes(request, SCOPE_ARTIFACTS_READ)
    if not valid_artifact_ref(manifest_id, artifact_id):
        # Refused before it can become a key, and reported as absent rather than as
        # malformed: which references are well-formed is not a caller's business.
        raise HTTPException(status_code=404, detail="not_found")

    settings = request.app.state.settings
    content = await read_artifact(
        get_object_store(settings),
        tenant_id=tenant_id_from_request(request),
        manifest_id=manifest_id,
        artifact_id=artifact_id,
    )
    if content is None:
        raise HTTPException(status_code=404, detail="not_found")
    return {
        "artifact_id": artifact_id,
        "manifest_id": manifest_id,
        "chars": len(content),
        "content": content,
    }
