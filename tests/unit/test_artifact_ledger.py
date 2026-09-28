"""Spilled tool outputs are recorded, so retention can find them; the rules that makes work.

The sweep itself is covered against both backends in `tests/conformance/test_retention.py`.
These pin what that contract takes for granted: the default is a bound rather than
keep-forever, and the ledger row is written before the bytes, so a failure leaves nothing
the sweep cannot name.
"""

from __future__ import annotations

import pytest
from felix import artifacts
from felix.artifacts import apply_artifact_spill, delete_artifact, expired_artifacts
from felix.config import Settings
from felix.jobs.retention import DAY_MS, Cutoffs
from felix.manifests.schema import ArtifactsSpec
from felix.tools.types import ToolInvocationCtx, define_tool, tool_output_content

SPEC = ArtifactsSpec(enabled=True, threshold_chars=100, preview_chars=10)
CTX = ToolInvocationCtx(thread_id="t")


class _Store:
    def __init__(self, *, fail_put: bool = False) -> None:
        self.objects: dict[str, bytes] = {}
        self.fail_put = fail_put

    async def get(self, key: str) -> bytes | None:
        return self.objects.get(key)

    async def put(self, key: str, data: bytes, *, content_type: str = "") -> None:
        if self.fail_put:
            raise OSError("disk full")
        self.objects[key] = data

    async def delete(self, key: str) -> None:
        self.objects.pop(key, None)


@pytest.fixture(autouse=True)
def _empty_ledger():
    artifacts.clear_memory_ledger()
    yield
    artifacts.clear_memory_ledger()


def _spilling(store: _Store, settings: Settings):
    async def handler(args: dict, ctx: object = None) -> str:
        # Two UTF-8 bytes per character, so a size recorded in characters is caught.
        return "é" * 1000

    (tool,) = apply_artifact_spill(
        [define_tool(name="dump", description="d", handler=handler)],
        SPEC,
        object_store=store,
        tenant_id="acme",
        manifest_id="cowork",
        settings=settings,
    )
    return tool


def test_spill_is_kept_thirty_days_by_default() -> None:
    # Unlike uploads (0 = keep). A spill is the harness's working copy and five bundled
    # manifests spill by default, so keep-forever would be growth nobody opted into.
    # Read off the field, not `Settings()`: that reads `.env`, where an operator may have set it.
    assert Settings.model_fields["artifact_retention_days"].default == 30
    settings = Settings().model_copy(update={"artifact_retention_days": 30})
    now = 1_900_000_000_000
    assert Cutoffs.from_settings(settings, now).artifact == now - 30 * DAY_MS
    assert (
        Cutoffs.from_settings(settings.model_copy(update={"artifact_retention_days": 0}), now).artifact
        is None
    )


@pytest.mark.asyncio
async def test_every_spill_is_recorded_with_its_size() -> None:
    store, settings = _Store(), Settings()

    await _spilling(store, settings).executor.execute({}, CTX)

    ((tenant, manifest, artifact_id),) = await expired_artifacts(settings, older_than_ms=2**62)
    assert (tenant, manifest) == ("acme", "cowork")
    assert f"artifacts/acme/cowork/{artifact_id}.txt" in store.objects
    assert artifacts._ledger_rows[(tenant, manifest, artifact_id)]["size_bytes"] == 2000


@pytest.mark.asyncio
async def test_a_failed_write_leaves_a_row_not_orphaned_bytes() -> None:
    # The recoverable direction: the row names objects that may not exist, and deleting an
    # absent key is a no-op. The model still gets a truncated result rather than an error.
    store, settings = _Store(fail_put=True), Settings()

    output = tool_output_content(await _spilling(store, settings).executor.execute({}, CTX))

    assert "artifact store write failed" in output
    assert store.objects == {}
    ((tenant, manifest, artifact_id),) = await expired_artifacts(settings, older_than_ms=2**62)
    assert await delete_artifact(
        store, tenant_id=tenant, manifest_id=manifest, artifact_id=artifact_id, settings=settings
    )
    assert await expired_artifacts(settings, older_than_ms=2**62) == []


@pytest.mark.asyncio
async def test_delete_takes_both_objects_and_the_row() -> None:
    store, settings = _Store(), Settings()
    await _spilling(store, settings).executor.execute({}, CTX)
    ((tenant, manifest, artifact_id),) = await expired_artifacts(settings, older_than_ms=2**62)
    assert len(store.objects) == 2, "the text and its owner record"

    assert await delete_artifact(
        store, tenant_id=tenant, manifest_id=manifest, artifact_id=artifact_id, settings=settings
    )

    assert store.objects == {}
    assert await expired_artifacts(settings, older_than_ms=2**62) == []


@pytest.mark.asyncio
async def test_delete_refuses_a_reference_that_is_not_an_artifact() -> None:
    store, settings = _Store(), Settings()
    store.objects["artifacts/acme/cowork/../../other/secret.txt"] = b"x"

    assert not await delete_artifact(
        store, tenant_id="acme", manifest_id="cowork", artifact_id="../../other/secret", settings=settings
    )
    assert store.objects == {"artifacts/acme/cowork/../../other/secret.txt": b"x"}, "nothing deleted"


# --- the sweep's drain ------------------------------------------------------------------


async def _seed(settings: Settings, store, n: int, *, at_ms: int, monkeypatch) -> list[str]:
    """`n` spills recorded at `at_ms`, each with both objects in `store`."""
    import secrets

    monkeypatch.setattr(artifacts, "now_ms", lambda: at_ms)
    ids = []
    for _ in range(n):
        artifact_id = secrets.token_hex(16)
        await artifacts.record_artifact(
            settings, tenant_id="acme", manifest_id="cowork", artifact_id=artifact_id, size_bytes=1
        )
        await store.put(f"artifacts/acme/cowork/{artifact_id}.txt", b"t")
        await store.put(f"artifacts/acme/cowork/{artifact_id}.owner", b"o")
        ids.append(artifact_id)
    return ids


@pytest.mark.asyncio
async def test_the_sweep_drains_past_one_batch(monkeypatch) -> None:
    from felix.jobs import retention

    store, settings = _Store(), Settings()
    monkeypatch.setattr("felix.storage.get_object_store", lambda _s: store)
    monkeypatch.setattr(retention, "OBJECT_SWEEP_BATCH", 2)
    await _seed(settings, store, 5, at_ms=1_000, monkeypatch=monkeypatch)

    assert await retention._sweep_artifacts(settings, cutoff=2_000) == 5
    assert store.objects == {}
    assert await expired_artifacts(settings, older_than_ms=2**62) == []


@pytest.mark.asyncio
async def test_a_row_that_names_no_valid_key_is_dropped_not_retried(monkeypatch) -> None:
    # Such a row can name no object, and kept it would head every batch forever.
    from felix.jobs import retention

    store, settings = _Store(), Settings()
    monkeypatch.setattr("felix.storage.get_object_store", lambda _s: store)
    monkeypatch.setattr(artifacts, "now_ms", lambda: 1_000)
    await artifacts.record_artifact(
        settings, tenant_id="acme", manifest_id="cowork", artifact_id="not-an-id", size_bytes=1
    )

    assert await retention._sweep_artifacts(settings, cutoff=2_000) == 0
    assert await expired_artifacts(settings, older_than_ms=2**62) == []


@pytest.mark.asyncio
async def test_a_store_that_refuses_deletes_stops_the_sweep_after_one_batch(monkeypatch) -> None:
    # Every failed delete keeps its row, so the next read would return the same batch. The
    # sweep must notice it made no progress rather than spend its bound re-reading it.
    from felix.jobs import retention

    store, settings = _Store(), Settings()
    monkeypatch.setattr("felix.storage.get_object_store", lambda _s: store)
    monkeypatch.setattr(retention, "OBJECT_SWEEP_BATCH", 2)
    ids = await _seed(settings, store, 5, at_ms=1_000, monkeypatch=monkeypatch)

    async def refuse(key: str) -> None:
        raise PermissionError("AccessDenied: s3:DeleteObject")

    monkeypatch.setattr(store, "delete", refuse)
    reads = 0
    real = retention.__dict__.get("expired_artifacts") or artifacts.expired_artifacts

    async def counting(*args, **kwargs):
        nonlocal reads
        reads += 1
        return await real(*args, **kwargs)

    monkeypatch.setattr(artifacts, "expired_artifacts", counting)

    assert await retention._sweep_artifacts(settings, cutoff=2_000) == 0
    assert reads == 1, f"re-read a batch it could not collect {reads} times"
    assert {r[2] for r in await expired_artifacts(settings, older_than_ms=2**62)} == set(ids)
