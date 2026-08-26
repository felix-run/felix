"""A spilled tool output can be read back, and only by the tenant that owns it.

The spill always worked and nothing ever read it: `object_store.get` was called
nowhere in the harness, so an oversized result was written to a store that no route,
CLI command or client method could reach. The model was handed a marker naming an
object nobody could fetch.

The reference is half caller-supplied and half not, and that split is what these
cover. The manifest is a path segment because the key needs it; the tenant comes
from credentials, so no spelling of the rest reaches another tenant's data.
"""

from __future__ import annotations

import pytest
from felix.artifacts import apply_artifact_spill, artifact_key, read_artifact, valid_artifact_ref
from felix.manifests.schema import ArtifactsSpec
from felix.tools.types import define_tool

ID = "0" * 32


class _Store:
    """An object store that records what it was asked for."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.asked: list[str] = []

    async def get(self, key: str) -> bytes | None:
        self.asked.append(key)
        return self.objects.get(key)

    async def put(self, key: str, data: bytes, *, content_type: str = "") -> None:
        self.objects[key] = data


@pytest.mark.asyncio
async def test_a_spilled_output_reads_back_whole() -> None:
    # The round trip that never existed: write via the spill, read via the reader,
    # rather than asserting the reader against a key spelled out by hand.
    store = _Store()
    big = "x" * 5000

    async def handler(args: dict, ctx: object = None) -> str:
        return big

    tool = define_tool(name="dump", description="d", handler=handler)
    (wrapped,) = apply_artifact_spill(
        [tool],
        ArtifactsSpec(enabled=True, threshold_chars=100, preview_chars=10),
        object_store=store,
        tenant_id="acme",
        manifest_id="cowork",
    )
    preview = await wrapped.executor.execute({})

    assert big not in str(preview), "the transcript keeps a preview, not the whole thing"
    (key,) = store.objects
    artifact_id = key.rsplit("/", 1)[1].removesuffix(".txt")

    content = await read_artifact(store, tenant_id="acme", manifest_id="cowork", artifact_id=artifact_id)
    assert content == big


@pytest.mark.asyncio
async def test_another_tenant_cannot_reach_it() -> None:
    store = _Store()
    store.objects[artifact_key("acme", "cowork", ID)] = b"secret"

    assert await read_artifact(store, tenant_id="acme", manifest_id="cowork", artifact_id=ID) == ("secret")
    # Same reference, different caller. The tenant is not a path segment, so this is
    # a different key rather than a permission check that could be forgotten.
    assert await read_artifact(store, tenant_id="other", manifest_id="cowork", artifact_id=ID) is None


@pytest.mark.asyncio
async def test_a_traversing_reference_never_becomes_a_key() -> None:
    store = _Store()
    store.objects["artifacts/acme/cowork/secret.txt"] = b"secret"

    for manifest_id, artifact_id in (
        ("../acme", ID),
        ("cowork", "../../acme/cowork/secret"),
        ("cowork/nested", ID),
        ("cowork", "not-a-uuid"),
        # No slash in them, which every traversal case tested alongside them had —
        # so the first spelling of the charset accepted these and the suite agreed
        # with the bug. Not a leak on any backend (the filesystem store rejects a
        # `..` part, and the rest treat the key as a literal), but a 500 on one and
        # a miss on the others is not what a bad reference should produce.
        ("..", ID),
        (".", ID),
        (".hidden", ID),
    ):
        assert not valid_artifact_ref(manifest_id, artifact_id)
        got = await read_artifact(store, tenant_id="evil", manifest_id=manifest_id, artifact_id=artifact_id)
        assert got is None, (manifest_id, artifact_id)

    assert store.asked == [], "a rejected reference must not reach the store at all"


@pytest.mark.asyncio
async def test_a_missing_artifact_is_absent_rather_than_an_error() -> None:
    assert await read_artifact(_Store(), tenant_id="acme", manifest_id="m", artifact_id=ID) is None


@pytest.mark.asyncio
async def test_no_object_store_configured_is_not_a_crash() -> None:
    # `apply_artifact_spill` is a no-op without a store, so a deployment can have
    # markers in old transcripts and no store now.
    assert await read_artifact(None, tenant_id="acme", manifest_id="m", artifact_id=ID) is None


@pytest.mark.asyncio
async def test_undecodable_bytes_come_back_rather_than_raising() -> None:
    # Spilled output is whatever a tool returned. A stored object that is not valid
    # UTF-8 should degrade, not 500 the read.
    store = _Store()
    store.objects[artifact_key("acme", "m", ID)] = b"ok \xff\xfe"
    content = await read_artifact(store, tenant_id="acme", manifest_id="m", artifact_id=ID)
    assert content is not None and content.startswith("ok ")


def test_a_dot_segment_cannot_escape_the_tenant_prefix() -> None:
    """The property the reference format exists to guarantee, asserted directly.

    Stated as a key rather than a read, because it is a claim about where a
    reference can *point* — not about whether an object happens to be there.
    """
    from felix.artifacts import _contained

    assert _contained("acme", artifact_key("acme", "cowork", ID))
    # What the escape would have produced: normalises to `artifacts/<id>.txt`, out
    # of `artifacts/acme/` entirely.
    assert not _contained("acme", artifact_key("acme", "..", ID))
    assert not valid_artifact_ref("..", ID)


class TestFilesystemStoreKeys:
    """The filesystem store validates a key by allowlist, not by screening for `..`.

    It screened only for `..` before, which left every other way a segment can fail
    to be a name — a newline, a NUL, a space, a leading dash — accepted and handed
    to the filesystem. This is the sink CodeQL names for the artifact read path, and
    it is shared: instruction-file keys from a manifest reach it too.
    """

    def _store(self, tmp_path):
        from felix.storage.fs import FilesystemObjectStore

        class _S:
            object_store_path = str(tmp_path)
            data_dir = str(tmp_path)

        return FilesystemObjectStore(_S())

    def test_it_accepts_the_keys_the_harness_actually_uses(self, tmp_path) -> None:
        import pytest as _pytest

        store = self._store(tmp_path)
        for key in (
            f"artifacts/acme/cowork/{ID}.txt",
            "workspace/acme/AGENTS.md",
            "__felix_readiness_probe__",
            ".agents.md",
        ):
            try:
                store._path(key)
            except ValueError:  # pragma: no cover - the assertion is the message
                _pytest.fail(f"rejected a key the harness uses: {key!r}")

    def test_it_refuses_a_segment_that_is_not_a_name(self, tmp_path) -> None:
        import pytest as _pytest

        store = self._store(tmp_path)
        for key in (
            "artifacts/../etc/passwd",
            "artifacts/./x",
            "artifacts/a b/x",
            "artifacts/a\nb/x",
            "artifacts/a\x00b/x",
            "",
            "/",
        ):
            with _pytest.raises(ValueError):
                store._path(key)

    def test_a_valid_key_still_lands_under_the_root(self, tmp_path) -> None:
        store = self._store(tmp_path)
        path = store._path("artifacts/acme/cowork/x.txt")
        assert path.is_relative_to(tmp_path.resolve())
