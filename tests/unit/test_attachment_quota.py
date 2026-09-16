"""A ceiling on what one tenant may store, and a sweep that can collect it.

`MAX_ATTACHMENT_BYTES` caps one upload and nothing capped how many — the same gap that
produced `documents_max_per_tenant`, because a per-request cap is not a per-tenant cap. The
security review of #239 named this as the condition on granting `files:write` to an
untrusted tenant, and #251 shipped the consuming half without it.

Neither half works without the ledger: the `ObjectStore` Protocol has no `list`, so nothing
could count what a tenant had stored or find what was old enough to drop. `attachments/` was
a prefix that only ever grew, on a disk shared with artifact spill and manifest storage.

Everything here runs through `create_app` where a scope or a status code is the subject, and
against the module where the subject is the ledger itself.
"""

from __future__ import annotations

import base64

import pytest
from felix.attachments import (
    MAX_ATTACHMENT_BYTES,
    QuotaExceeded,
    clear_memory_ledger,
    delete_attachment,
    expired_attachments,
    put_attachment,
    tenant_attachment_bytes,
)
from felix.config import Settings
from felix.jobs import retention
from felix.storage import get_object_store
from httpx import ASGITransport, AsyncClient

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"x" * 64
PNG = base64.b64encode(PNG_BYTES).decode("ascii")

KEYS = (
    '{"sk-rw":{"tenant_id":"acme","sub":"ops","scopes":["files:read","files:write"]},'
    '"sk-other":{"tenant_id":"globex","sub":"ops","scopes":["files:read","files:write"]}}'
)


@pytest.fixture(autouse=True)
def _clean_ledger() -> None:
    clear_memory_ledger()


def _settings(**kw: object) -> Settings:
    base: dict[str, object] = {
        "allow_insecure": True,
        "auth_mode": "api_key",
        "auth_api_keys": KEYS,
        "environment": "development",
        "object_store": "memory",
        "database_url": "memory://quota",
    }
    base.update(kw)
    return Settings(**base)  # type: ignore[arg-type]


async def _client(**kw: object) -> tuple[AsyncClient, Settings]:
    from felix_api.app import create_app

    settings = _settings(**kw)
    app = create_app(settings=settings, plugins=[])
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test"), settings


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


async def _store(settings: Settings, tenant_id: str, raw: bytes = PNG_BYTES) -> str:
    stored = await put_attachment(
        get_object_store(settings),
        tenant_id=tenant_id,
        data=raw,
        media_type="image/png",
        settings=settings,
    )
    return stored.file_id


@pytest.mark.asyncio
async def test_an_upload_over_the_ceiling_is_refused_with_409() -> None:
    """409 rather than 413: the request is a fine size and the account is full.

    A 413 sends the caller off to shrink an image that was never the problem.
    """
    client, _ = await _client(attachments_max_bytes_per_tenant=len(PNG_BYTES) + 1)
    async with client:
        first = await client.post(
            "/files", json={"data": PNG, "media_type": "image/png"}, headers=_auth("sk-rw")
        )
        assert first.status_code == 200, first.text

        second = await client.post(
            "/files", json={"data": PNG, "media_type": "image/png"}, headers=_auth("sk-rw")
        )
        assert second.status_code == 409, second.text
        assert "FELIX_ATTACHMENTS_MAX_BYTES_PER_TENANT" in second.json()["detail"]


@pytest.mark.asyncio
async def test_the_refused_upload_stored_nothing() -> None:
    """Checked before the object is written, so a refusal leaves no bytes behind — the
    ceiling would otherwise be the thing that filled the disk it exists to protect."""
    client, settings = await _client(attachments_max_bytes_per_tenant=len(PNG_BYTES) + 1)
    async with client:
        await client.post("/files", json={"data": PNG, "media_type": "image/png"}, headers=_auth("sk-rw"))
        await client.post("/files", json={"data": PNG, "media_type": "image/png"}, headers=_auth("sk-rw"))

    assert await tenant_attachment_bytes(settings, "acme") == len(PNG_BYTES)


@pytest.mark.asyncio
async def test_the_ceiling_is_per_tenant_not_per_deployment() -> None:
    """Otherwise the first tenant to fill it denies service to every other one."""
    client, settings = await _client(attachments_max_bytes_per_tenant=len(PNG_BYTES) + 1)
    async with client:
        mine = await client.post(
            "/files", json={"data": PNG, "media_type": "image/png"}, headers=_auth("sk-rw")
        )
        theirs = await client.post(
            "/files", json={"data": PNG, "media_type": "image/png"}, headers=_auth("sk-other")
        )
        assert (mine.status_code, theirs.status_code) == (200, 200), theirs.text

    assert await tenant_attachment_bytes(settings, "acme") == len(PNG_BYTES)
    assert await tenant_attachment_bytes(settings, "globex") == len(PNG_BYTES)


@pytest.mark.asyncio
async def test_deleting_an_upload_returns_its_bytes_to_the_tenant() -> None:
    """The ledger has to shrink, or a tenant that deletes everything stays locked out."""
    settings = _settings(attachments_max_bytes_per_tenant=len(PNG_BYTES) + 1)
    file_id = await _store(settings, "acme")
    assert await tenant_attachment_bytes(settings, "acme") == len(PNG_BYTES)

    await delete_attachment(get_object_store(settings), tenant_id="acme", file_id=file_id, settings=settings)

    assert await tenant_attachment_bytes(settings, "acme") == 0
    await _store(settings, "acme")  # room again; raises QuotaExceeded if the row survived


@pytest.mark.asyncio
async def test_a_zero_ceiling_is_no_ceiling() -> None:
    """`0` disables it, which is what a deployment that has not thought about this gets."""
    settings = _settings(attachments_max_bytes_per_tenant=0)
    for _ in range(4):
        await _store(settings, "acme")
    assert await tenant_attachment_bytes(settings, "acme") == 4 * len(PNG_BYTES)


@pytest.mark.asyncio
async def test_the_ceiling_counts_decoded_bytes_not_the_base64_that_carried_them() -> None:
    """base64 inflates by a third, and that is a property of the request rather than of the
    object on disk. Counting the wire form would bill a tenant for transport."""
    settings = _settings()
    await _store(settings, "acme")
    assert await tenant_attachment_bytes(settings, "acme") == len(PNG_BYTES)
    assert len(PNG) > len(PNG_BYTES)


@pytest.mark.asyncio
async def test_storing_without_settings_is_not_possible() -> None:
    """`settings` is required, and that is the whole of the quota's integrity.

    An upload stored with no ledger row is not merely unbilled — `expired_attachments` reads
    rows too, so nothing in the system could ever name or collect it, on a store whose
    Protocol has no `list`. A parameter whose omission produces that is not a quota; it is
    this repo's silent-default shape pointed at a control. Both reviewers said so.
    """
    settings = _settings()
    with pytest.raises(TypeError):
        await put_attachment(  # type: ignore[call-arg]
            get_object_store(settings), tenant_id="acme", data=PNG_BYTES, media_type="image/png"
        )
    assert await tenant_attachment_bytes(settings, "acme") == 0


@pytest.mark.asyncio
async def test_put_attachment_raises_quota_exceeded_rather_than_a_bare_error() -> None:
    """The route branches on the type to answer 409, so the distinction is load-bearing."""
    settings = _settings(attachments_max_bytes_per_tenant=len(PNG_BYTES))
    await _store(settings, "acme")
    with pytest.raises(QuotaExceeded):
        await _store(settings, "acme")


@pytest.mark.asyncio
async def test_one_upload_can_never_exceed_a_ceiling_it_is_allowed_to_reach() -> None:
    """A ceiling below `MAX_ATTACHMENT_BYTES` would refuse a first upload that the per-file
    cap admits, so the two limits have to be read together rather than separately."""
    settings = _settings(attachments_max_bytes_per_tenant=MAX_ATTACHMENT_BYTES)
    await _store(settings, "acme", b"\x89PNG\r\n\x1a\n" + b"y" * (MAX_ATTACHMENT_BYTES - 8))
    assert await tenant_attachment_bytes(settings, "acme") == MAX_ATTACHMENT_BYTES


@pytest.mark.asyncio
async def test_the_sweep_collects_the_bytes_and_the_row_together() -> None:
    """`attachments/` was an object-store prefix nothing ever collected.

    Dropping only the row would orphan the bytes for good — nothing else lists that prefix,
    which is the reason the ledger exists at all.
    """
    settings = _settings(attachment_retention_days=1)
    file_id = await _store(settings, "acme")
    store = get_object_store(settings)
    key = f"attachments/acme/{file_id}"
    assert await store.get(key) == PNG_BYTES

    # Age it past the cutoff by rewriting the ledger's own clock.
    from felix import attachments as att_mod

    att_mod._ledger_rows[("acme", file_id)]["created_at"] = 0

    counts = await retention.run_retention_sweep(settings)

    assert counts["attachments"] == 1
    assert await tenant_attachment_bytes(settings, "acme") == 0
    assert await store.get(key) is None, "the row went and the bytes stayed"


@pytest.mark.asyncio
async def test_the_sweep_keeps_everything_when_retention_is_off() -> None:
    """`0` days is the default, and deleting caller data on a timer is an operator's
    decision rather than ours."""
    settings = _settings(attachment_retention_days=0)
    file_id = await _store(settings, "acme")
    from felix import attachments as att_mod

    att_mod._ledger_rows[("acme", file_id)]["created_at"] = 0

    counts = await retention.run_retention_sweep(settings)

    assert counts["attachments"] == 0
    assert await tenant_attachment_bytes(settings, "acme") == len(PNG_BYTES)


@pytest.mark.asyncio
async def test_the_sweep_leaves_an_upload_that_is_young_enough() -> None:
    settings = _settings(attachment_retention_days=1)
    await _store(settings, "acme")

    counts = await retention.run_retention_sweep(settings)

    assert counts["attachments"] == 0
    assert await tenant_attachment_bytes(settings, "acme") == len(PNG_BYTES)


@pytest.mark.asyncio
async def test_expired_attachments_crosses_tenants_because_the_sweep_does() -> None:
    """It runs in the worker with no request and no principal, so a per-tenant read would
    return nothing and the sweep would silently collect nothing."""
    settings = _settings()
    await _store(settings, "acme")
    await _store(settings, "globex")
    from felix import attachments as att_mod

    for row in att_mod._ledger_rows.values():
        row["created_at"] = 0

    found = await expired_attachments(settings, older_than_ms=1)

    assert {tenant for tenant, _ in found} == {"acme", "globex"}


@pytest.mark.asyncio
async def test_a_failed_object_write_does_not_leave_the_tenant_billed() -> None:
    """The row goes in first, so an ordinary store failure has to take it back out.

    Leaving it would charge a tenant for an upload that plainly did not happen. The row is
    only meant to outlive its bytes on a hard crash, where something visible beats something
    silent — not on a failure the code can see.
    """
    settings = _settings()
    store = get_object_store(settings)

    async def failing_put(*_a: object, **_k: object) -> None:
        raise RuntimeError("object store is down")

    original = store.put
    store.put = failing_put  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError):
            await _store(settings, "acme")
    finally:
        store.put = original  # type: ignore[method-assign]

    assert await tenant_attachment_bytes(settings, "acme") == 0


@pytest.mark.asyncio
async def test_a_failed_object_delete_keeps_the_row() -> None:
    """Bytes first, row second.

    If the bytes could not be deleted the row must stay, or the object becomes unreachable
    by the count and by the sweep at once — the orphan this ordering exists to prevent.
    """
    settings = _settings()
    file_id = await _store(settings, "acme")
    store = get_object_store(settings)

    async def failing_delete(*_a: object, **_k: object) -> None:
        raise RuntimeError("object store is down")

    original = store.delete
    store.delete = failing_delete  # type: ignore[method-assign]
    try:
        acted = await delete_attachment(store, tenant_id="acme", file_id=file_id, settings=settings)
    finally:
        store.delete = original  # type: ignore[method-assign]

    assert acted is False
    assert await tenant_attachment_bytes(settings, "acme") == len(PNG_BYTES), (
        "the row went while the bytes stayed: nothing can name them now"
    )


@pytest.mark.asyncio
async def test_the_sweep_drains_more_than_one_batch() -> None:
    """One batch per nightly run never catches up with a backlog bigger than it, and the
    count it returns looks like an ordinary number, so nothing would say so."""
    from felix import attachments as att_mod
    from felix.jobs import retention as ret_mod

    settings = _settings(attachment_retention_days=1)
    for _ in range(5):
        await _store(settings, "acme")
    for row in att_mod._ledger_rows.values():
        row["created_at"] = 0

    original = ret_mod.ATTACHMENT_SWEEP_BATCH
    ret_mod.ATTACHMENT_SWEEP_BATCH = 2
    try:
        counts = await retention.run_retention_sweep(settings)
    finally:
        ret_mod.ATTACHMENT_SWEEP_BATCH = original

    assert counts["attachments"] == 5, "the sweep stopped after one batch"
    assert await tenant_attachment_bytes(settings, "acme") == 0


@pytest.mark.asyncio
async def test_a_failed_ledger_write_stores_no_bytes_at_all() -> None:
    """The row goes in *before* the object, and this is the only way to see that from outside.

    Asserting the total is zero after a failed upload does not distinguish the two orders —
    it is zero either way. What separates them is whether the bytes exist: object-first
    leaves them on disk with no row, invisible to `tenant_attachment_bytes` *and* to
    `expired_attachments`, on a store whose Protocol has no `list`. Nothing could ever name
    them again, and the quota would fail open, because a total that never grows never
    refuses anything.
    """
    from felix import attachments as att_mod

    settings = _settings()
    store = get_object_store(settings)
    put_calls: list[str] = []

    original_put = store.put
    original_record = att_mod.record_attachment

    async def watching_put(key: str, *a: object, **k: object) -> None:
        put_calls.append(key)
        return await original_put(key, *a, **k)

    async def failing_record(*_a: object, **_k: object) -> None:
        raise RuntimeError("database is down")

    store.put = watching_put  # type: ignore[method-assign]
    att_mod.record_attachment = failing_record  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError):
            await _store(settings, "acme")
    finally:
        store.put = original_put  # type: ignore[method-assign]
        att_mod.record_attachment = original_record  # type: ignore[assignment]

    assert put_calls == [], "bytes were written before the ledger knew about them"
