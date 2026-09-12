"""Upload a file once, reference it by id on later turns.

An image sent inline lands in the session event log, and `full_replay` sends that log
again on every subsequent turn — so the bytes are re-uploaded to the model for the life of
the thread. Stored here instead, a message carries a reference small enough to replay.

Shaped on `routes/artifacts.py`, and for its reasons: **the tenant comes from the caller's
own credentials and never from the path**, so no spelling of a reference reaches another
tenant's data; a malformed reference is a 404 rather than a 400, because which ids are
well-formed is not a caller's business; and both endpoints are scope-gated.
"""

from __future__ import annotations

import base64
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from felix.auth.mgmt import (
    SCOPE_FILES_READ,
    SCOPE_FILES_WRITE,
    require_mgmt_scopes,
    tenant_id_from_request,
)
from pydantic import BaseModel, Field

router = APIRouter(tags=["Files"])


class UploadRequest(BaseModel):
    model_config = {"extra": "forbid"}

    data: str = Field(description="The file's bytes, base64-encoded.")
    media_type: str = Field(max_length=127, description="An allowed image media type, e.g. image/png.")
    filename: str = Field(default="", max_length=255)


@router.post("")
@router.post("/")
async def upload_file(body: UploadRequest, request: Request) -> dict[str, Any]:
    """Store bytes and return the id a later message can reference."""
    from felix.attachments import AttachmentError, decode_upload, put_attachment
    from felix.storage import get_object_store

    require_mgmt_scopes(request, SCOPE_FILES_WRITE)
    try:
        raw = decode_upload(body.data, body.media_type)
    except AttachmentError as exc:
        # 400 here, unlike a bad *reference*: the caller sent this content in this request
        # and can fix it, and saying which of size or type was wrong is the whole value.
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    settings = request.app.state.settings
    try:
        stored = await put_attachment(
            get_object_store(settings),
            tenant_id=tenant_id_from_request(request),
            data=raw,
            media_type=body.media_type,
            filename=body.filename,
        )
    except AttachmentError as exc:
        # No object store configured is the deployment's problem, not the caller's.
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return {
        "file_id": stored.file_id,
        "media_type": stored.media_type,
        "size_bytes": stored.size_bytes,
        "filename": stored.filename,
    }


@router.get("/{file_id}")
async def get_file(file_id: str, request: Request) -> dict[str, Any]:
    """Read an upload back, base64-encoded as it was sent.

    Returned in the request's own shape rather than as raw bytes with a content type: this
    is a management surface for checking what was stored, not a CDN, and handing back an
    attacker-supplied media type on a raw body is a content-sniffing problem nobody needs.
    """
    from felix.attachments import read_attachment
    from felix.storage import get_object_store

    require_mgmt_scopes(request, SCOPE_FILES_READ)
    settings = request.app.state.settings
    raw = await read_attachment(
        get_object_store(settings),
        tenant_id=tenant_id_from_request(request),
        file_id=file_id,
    )
    if raw is None:
        raise HTTPException(status_code=404, detail="not_found")
    return {
        "file_id": file_id,
        "size_bytes": len(raw),
        "data": base64.b64encode(raw).decode("ascii"),
    }


@router.delete("/{file_id}")
async def delete_file(file_id: str, request: Request) -> dict[str, Any]:
    """Remove an upload.

    Under `files:write`, not `files:read`: removing is a write. Everything stored here is
    caller-supplied by construction, so without this an operator has no way to answer a
    deletion request — and the surface could only ever accumulate.

    Answers the same for "already gone" and "never yours", so it is idempotent and is not
    an oracle for which ids exist.
    """
    from felix.attachments import delete_attachment
    from felix.storage import get_object_store

    require_mgmt_scopes(request, SCOPE_FILES_WRITE)
    settings = request.app.state.settings
    await delete_attachment(
        get_object_store(settings),
        tenant_id=tenant_id_from_request(request),
        file_id=file_id,
    )
    return {"file_id": file_id, "deleted": True}
