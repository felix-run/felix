"""Uploading a file, and the boundaries that make a reference safe to hand out.

An image sent inline lands in the session event log and `full_replay` re-sends that log on
every later turn, so the bytes are re-uploaded to the model for the life of the thread. The
point of storing one is that a message can carry a reference instead.

Everything here runs through `create_app`, because the parts worth testing are the ones a
direct call to the module would skip: the scope gate, and where the tenant comes from.
"""

from __future__ import annotations

import base64

import pytest
from felix.config import Settings
from httpx import ASGITransport, AsyncClient

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * 64).decode("ascii")

KEYS = (
    '{"sk-rw":{"tenant_id":"acme","sub":"ops","scopes":["files:read","files:write"]},'
    '"sk-ro":{"tenant_id":"acme","sub":"ops","scopes":["files:read"]},'
    '"sk-none":{"tenant_id":"acme","sub":"ops","scopes":["chat:write"]},'
    '"sk-other":{"tenant_id":"globex","sub":"ops","scopes":["files:read","files:write"]}}'
)


def _settings(**kw: object) -> Settings:
    base: dict[str, object] = {
        "allow_insecure": True,
        "auth_mode": "api_key",
        "auth_api_keys": KEYS,
        "environment": "development",
        "object_store": "memory",
        "database_url": "memory://files",
    }
    base.update(kw)
    return Settings(**base)  # type: ignore[arg-type]


async def _client(**kw: object) -> tuple[AsyncClient, object]:
    from felix_api.app import create_app

    app = create_app(settings=_settings(**kw), plugins=[])
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test"), app


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.mark.asyncio
async def test_an_upload_round_trips() -> None:
    client, _ = await _client()
    async with client:
        up = await client.post(
            "/files",
            json={"data": PNG, "media_type": "image/png", "filename": "shot.png"},
            headers=_auth("sk-rw"),
        )
        assert up.status_code == 200, up.text
        body = up.json()
        assert len(body["file_id"]) == 32, "the id is a uuid4 hex, so it cannot be a path"
        assert body["size_bytes"] == len(base64.b64decode(PNG))
        assert body["filename"] == "shot.png"

        back = await client.get(f"/files/{body['file_id']}", headers=_auth("sk-rw"))
        assert back.status_code == 200
        assert back.json()["data"] == PNG


@pytest.mark.asyncio
async def test_each_endpoint_gates_on_its_own_scope() -> None:
    """Reading an upload and creating one are separate grants.

    `files:read` on its own must not let a caller write, which is the half a single
    `files` scope would have collapsed.
    """
    client, _ = await _client()
    async with client:
        denied = await client.post(
            "/files", json={"data": PNG, "media_type": "image/png"}, headers=_auth("sk-ro")
        )
        assert denied.status_code == 403
        assert "files:write" in denied.json()["detail"]

        none = await client.get("/files/" + "0" * 32, headers=_auth("sk-none"))
        assert none.status_code == 403


@pytest.mark.asyncio
async def test_one_tenant_cannot_read_another_s_upload() -> None:
    """The tenant comes from the caller's credentials and is never in the path.

    So there is no reference a caller can spell that reaches someone else's bytes — the
    same id under a different credential simply does not resolve.
    """
    client, _ = await _client()
    async with client:
        up = await client.post(
            "/files", json={"data": PNG, "media_type": "image/png"}, headers=_auth("sk-rw")
        )
        file_id = up.json()["file_id"]

        mine = await client.get(f"/files/{file_id}", headers=_auth("sk-rw"))
        assert mine.status_code == 200

        theirs = await client.get(f"/files/{file_id}", headers=_auth("sk-other"))
        assert theirs.status_code == 404, "a valid id resolved under the wrong tenant"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "file_id",
    [
        pytest.param("../../etc/passwd", id="traversal"),
        pytest.param("not-hex", id="wrong-charset"),
        pytest.param("0" * 31, id="too-short"),
        pytest.param("0" * 33, id="too-long"),
        pytest.param("0" * 32, id="well-formed-but-absent"),
    ],
)
async def test_a_reference_that_names_nothing_is_a_404(file_id: str) -> None:
    """Absent and malformed answer alike, on purpose.

    Which references are well-formed is not a caller's business, and a 400 for a bad shape
    plus a 404 for a good one is an oracle for the id format.
    """
    client, _ = await _client()
    async with client:
        got = await client.get(f"/files/{file_id}", headers=_auth("sk-rw"))
        assert got.status_code == 404, f"{file_id!r} answered {got.status_code}"


@pytest.mark.asyncio
async def test_a_type_the_harness_cannot_show_a_model_is_refused() -> None:
    """An allowlist, because the bytes end up at a vendor API that decides for itself.

    400 rather than 404 here: the caller sent this content in this request and can fix it,
    and naming the allowed set is the whole value of the refusal.
    """
    client, _ = await _client()
    async with client:
        got = await client.post(
            "/files",
            json={"data": PNG, "media_type": "application/x-msdownload"},
            headers=_auth("sk-rw"),
        )
        assert got.status_code == 400
        assert "image/png" in got.json()["detail"], "the refusal should name what is allowed"


@pytest.mark.asyncio
async def test_an_oversized_attachment_is_refused_below_the_body_limit() -> None:
    """The ceiling is the decoded size, and it sits under the 1 MiB body limit.

    Measured after decoding because that is the size the model is charged for — and
    because a limit above the body limit advertises a size the middleware answers 413 to
    before the route is ever reached.
    """
    from felix.attachments import MAX_ATTACHMENT_BYTES

    oversized = base64.b64encode(b"x" * (MAX_ATTACHMENT_BYTES + 1)).decode("ascii")
    client, _ = await _client()
    async with client:
        got = await client.post(
            "/files", json={"data": oversized, "media_type": "image/png"}, headers=_auth("sk-rw")
        )
        assert got.status_code == 400
        assert str(MAX_ATTACHMENT_BYTES) in got.json()["detail"]


@pytest.mark.asyncio
async def test_a_body_that_is_not_base64_is_refused_rather_than_stored_truncated() -> None:
    """`validate=True`, so a bad body fails here instead of resolving to wrong bytes."""
    client, _ = await _client()
    async with client:
        got = await client.post(
            "/files", json={"data": "not base64 !!", "media_type": "image/png"}, headers=_auth("sk-rw")
        )
        assert got.status_code == 400
        assert "base64" in got.json()["detail"]


def test_a_reference_cannot_escape_its_tenant_prefix() -> None:
    """The containment check, asserted directly as well as through the route.

    `felix.artifacts` carries the reason: a charset check is an *argument* that traversal
    is impossible, and that argument has already been wrong once in this repo — `..`
    passed a pattern that allowed `.` anywhere.
    """
    from felix.attachments import attachment_key, valid_file_id

    assert valid_file_id("0" * 32) is True
    for hostile in ("..", "../..", "a/b", "0" * 31 + "/", ""):
        assert valid_file_id(hostile) is False, f"{hostile!r} was accepted as a file id"
    assert attachment_key("acme", "0" * 32).startswith("attachments/acme/")


@pytest.mark.asyncio
async def test_bytes_that_are_not_the_declared_type_are_refused() -> None:
    """The media type is a caller assertion and nothing downstream re-derives it.

    Unchecked, `/files` is a general-purpose blob host wearing an image allowlist — any
    bytes stored as `image/png`, and the default filesystem store discards the content
    type, so not even the claim survives. The follow-up resolver would then have to take
    the type from the caller's message, attacker-controlled twice over.
    """
    not_a_png = base64.b64encode(b"MZ\x90\x00" + b"x" * 64).decode("ascii")
    client, _ = await _client()
    async with client:
        got = await client.post(
            "/files", json={"data": not_a_png, "media_type": "image/png"}, headers=_auth("sk-rw")
        )
        assert got.status_code == 400
        assert "image/png" in got.json()["detail"]


@pytest.mark.asyncio
async def test_an_upload_can_be_deleted_and_deleting_twice_is_not_an_error() -> None:
    """The erasure path. Everything here is caller-supplied, so a surface that can only
    accumulate is one an operator cannot answer a deletion request with.

    Idempotent on purpose, and it answers the same for "already gone" as for "never
    yours", so it is not an oracle for which ids exist.
    """
    client, _ = await _client()
    async with client:
        up = await client.post(
            "/files", json={"data": PNG, "media_type": "image/png"}, headers=_auth("sk-rw")
        )
        file_id = up.json()["file_id"]

        first = await client.delete(f"/files/{file_id}", headers=_auth("sk-rw"))
        assert first.status_code == 200

        gone = await client.get(f"/files/{file_id}", headers=_auth("sk-rw"))
        assert gone.status_code == 404, "the bytes survived the delete"

        again = await client.delete(f"/files/{file_id}", headers=_auth("sk-rw"))
        assert again.status_code == 200, "a second delete should be a no-op, not an error"


@pytest.mark.asyncio
async def test_deleting_needs_the_write_scope() -> None:
    """Removing is a write, so `files:read` must not reach it."""
    client, _ = await _client()
    async with client:
        denied = await client.delete("/files/" + "0" * 32, headers=_auth("sk-ro"))
        assert denied.status_code == 403
        assert "files:write" in denied.json()["detail"]


@pytest.mark.asyncio
async def test_one_tenant_cannot_delete_another_s_upload() -> None:
    client, _ = await _client()
    async with client:
        up = await client.post(
            "/files", json={"data": PNG, "media_type": "image/png"}, headers=_auth("sk-rw")
        )
        file_id = up.json()["file_id"]

        theirs = await client.delete(f"/files/{file_id}", headers=_auth("sk-other"))
        assert theirs.status_code == 200, "the answer must not reveal whose id this is"

        mine = await client.get(f"/files/{file_id}", headers=_auth("sk-rw"))
        assert mine.status_code == 200, "another tenant's delete removed my file"


@pytest.mark.asyncio
async def test_an_unbounded_media_type_is_refused_before_it_is_reflected() -> None:
    """A media type is a token, not a payload. Unbounded, a ~1 MiB one fits inside the
    body limit and comes back in the refusal."""
    client, _ = await _client()
    async with client:
        got = await client.post(
            "/files",
            json={"data": PNG, "media_type": "image/" + "p" * 5000},
            headers=_auth("sk-rw"),
        )
        assert got.status_code == 422, "pydantic bounds the field before the route sees it"


@pytest.mark.asyncio
async def test_a_hostile_tenant_id_cannot_write_outside_its_prefix() -> None:
    """`put_attachment` is an exported API, and its callers may one day not be the doors.

    Every HTTP path validates the tenant through `Principal`, so this is unreachable today
    — but `read_artifact` checks the *real* tenant rather than a placeholder and this
    module claimed parity with it. Review proved the gap with `../../manifests/acme`.
    """
    from felix.attachments import AttachmentError, put_attachment, read_attachment
    from felix.storage import MemoryObjectStore

    store = MemoryObjectStore()
    with pytest.raises(AttachmentError):
        await put_attachment(
            store,
            tenant_id="../../manifests/acme",
            data=b"\x89PNG\r\n\x1a\n",
            media_type="image/png",
        )
    assert await read_attachment(store, tenant_id="../..", file_id="0" * 32) is None


def test_no_logged_value_can_forge_a_record(caplog: pytest.LogCaptureFixture) -> None:
    """A log line's separator is the newline, and this module logs three caller-shaped
    values: the tenant, the file id and the media type.

    `put_attachment` is an exported API — reached directly, it supplies all three, and the
    `media_type` never met `decode_upload`'s allowlist. "Validated elsewhere" is an
    argument that has to be re-made whenever the caller set changes, so every interpolated
    value goes through `loggable` instead. CodeQL flagged all four call sites.
    """
    import asyncio
    import logging

    from felix.attachments import put_attachment
    from felix.storage import MemoryObjectStore

    hostile = "acme\nWARNING  forged: all clear"
    with caplog.at_level(logging.INFO, logger="felix.attachments"):
        asyncio.run(
            put_attachment(
                MemoryObjectStore(),
                tenant_id="acme",
                data=b"\x89PNG\r\n\x1a\n",
                media_type=hostile,
            )
        )

    records = [r for r in caplog.records if r.name == "felix.attachments"]
    assert len(records) == 1, "the log line was split in two"
    message = records[0].getMessage()
    assert "\n" not in message
    assert "\\n" in message, "the newline should be shown, not silently stripped"
