"""The model can read back what `spec.artifacts` spilled.

Before this tool the spill replaced any large tool result with a 200-character preview and
nothing the model could call fetched the rest, so enabling it saved tokens by discarding what
the agent had asked for. These cover the reader against the real spill rather than against a
key spelled out by hand, because the two agreeing on the key is the thing that matters.
"""

from __future__ import annotations

import pytest
from felix.artifacts import (
    READ_ARTIFACT_TOOL,
    apply_artifact_spill,
    artifact_key,
    make_read_artifact_tool,
)
from felix.manifests.schema import ArtifactsSpec
from felix.tools.errors import read_tool_error_code
from felix.tools.types import define_tool, tool_output_content

BIG = "".join(f"line {i:05d}\n" for i in range(2000))  # 22,000 chars, all distinct
SPEC = ArtifactsSpec(
    enabled=True, threshold_chars=1000, preview_chars=50, default_window_chars=3000, max_window_chars=5000
)


class _Store:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def get(self, key: str) -> bytes | None:
        return self.objects.get(key)

    async def put(self, key: str, data: bytes, *, content_type: str = "") -> None:
        self.objects[key] = data


async def _spill(store: _Store, text: str = BIG) -> str:
    """Run `text` through the real spill; return the id the marker names."""

    async def handler(args: dict, ctx: object = None) -> str:
        return text

    (wrapped,) = apply_artifact_spill(
        [define_tool(name="dump", description="d", handler=handler)],
        SPEC,
        object_store=store,
        tenant_id="acme",
        manifest_id="cowork",
    )
    marker = str(await wrapped.executor.execute({}))
    # The marker names the call and the offset where the preview stopped, so the model's
    # first read picks up exactly where the preview left off.
    assert f'{READ_ARTIFACT_TOOL}(artifact_id="' in marker
    assert f"offset={min(len(text), SPEC.preview_chars)})" in marker
    (key,) = store.objects
    return key.rsplit("/", 1)[1].removesuffix(".txt")


def _reader(store: _Store, *, tenant: str = "acme", manifest: str = "cowork"):
    return make_read_artifact_tool(SPEC, object_store=store, tenant_id=tenant, manifest_id=manifest)


async def _read(tool, **args) -> str:
    return tool_output_content(await tool.executor.execute(args))


def _body(page: str) -> str:
    return page.split("\n", 1)[1]


@pytest.mark.asyncio
async def test_paging_from_the_offsets_it_reports_reconstructs_the_output() -> None:
    store = _Store()
    artifact_id = await _spill(store)
    reader = _reader(store)

    pages, offset = [], 0
    while True:
        page = await _read(reader, artifact_id=artifact_id, offset=offset)
        pages.append(_body(page))
        if page.startswith(f"[artifact-window:{artifact_id} chars {offset}-{len(BIG)} of {len(BIG)}; end]"):
            break
        assert "continue at offset=" in page.split("\n", 1)[0], f"unexpected header: {page[:80]!r}"
        offset = int(page.split("continue at offset=", 1)[1].split("]", 1)[0])
        assert len(pages) < 20, "the reader never reached the end"

    assert "".join(pages) == BIG
    assert len(pages[0]) == SPEC.default_window_chars, "no length means the default window"


@pytest.mark.asyncio
async def test_a_request_larger_than_the_cap_is_capped() -> None:
    store = _Store()
    artifact_id = await _spill(store)

    page = await _read(_reader(store), artifact_id=artifact_id, length=100_000)

    assert _body(page) == BIG[: SPEC.max_window_chars]


@pytest.mark.asyncio
async def test_an_offset_past_the_end_is_an_empty_last_page_not_an_error() -> None:
    store = _Store()
    artifact_id = await _spill(store)

    page = await _read(_reader(store), artifact_id=artifact_id, offset=len(BIG) + 10)

    assert page == f"[artifact-window:{artifact_id} chars {len(BIG)}-{len(BIG)} of {len(BIG)}; end]\n"


@pytest.mark.asyncio
async def test_the_reader_is_not_itself_spilled() -> None:
    # Its window (5000) is over the spill threshold (1000). Wrapped like any other tool, a
    # read would come back as a fresh preview of the text the model just asked to see.
    store = _Store()
    artifact_id = await _spill(store)
    (reader,) = apply_artifact_spill(
        [_reader(store)], SPEC, object_store=store, tenant_id="acme", manifest_id="cowork"
    )

    page = await _read(reader, artifact_id=artifact_id, length=5000)

    assert _body(page) == BIG[:5000]
    assert len(store.objects) == 1, "reading must not write a new artifact"


@pytest.mark.asyncio
async def test_a_manifest_tool_named_read_artifact_is_still_spilled() -> None:
    # The exemption is by source, so it cannot be claimed by picking a name.
    store = _Store()

    async def handler(args: dict, ctx: object = None) -> str:
        return BIG

    (impostor,) = apply_artifact_spill(
        [define_tool(name=READ_ARTIFACT_TOOL, description="d", handler=handler)],
        SPEC,
        object_store=store,
        tenant_id="acme",
        manifest_id="cowork",
    )

    assert BIG not in tool_output_content(await impostor.executor.execute({}))
    assert len(store.objects) == 1


@pytest.mark.asyncio
async def test_it_cannot_reach_another_tenant_or_manifest() -> None:
    # The model names only the id; tenant and manifest are fixed when the tool is built.
    store = _Store()
    artifact_id = "0" * 32
    store.objects[artifact_key("other", "cowork", artifact_id)] = b"their secret"
    store.objects[artifact_key("acme", "deep", artifact_id)] = b"another agent's output"

    output = await _reader(store).executor.execute({"artifact_id": artifact_id})

    assert read_tool_error_code(output) == "invalid_arguments"
    assert "secret" not in tool_output_content(output)
    assert "another agent" not in tool_output_content(output)


@pytest.mark.asyncio
@pytest.mark.parametrize("artifact_id", ["f" * 32, "not-an-id"])
async def test_an_unknown_or_malformed_id_is_a_tool_error(artifact_id: str) -> None:
    # Traversal spellings are covered where the id is validated, in test_artifact_read.py.
    output = await _reader(_Store()).executor.execute({"artifact_id": artifact_id})

    assert read_tool_error_code(output) == "invalid_arguments"
    assert artifact_id in tool_output_content(output)


# --- the binding, as `build_agent` does it --------------------------------------


@pytest.fixture
def _restore_patterns():
    from felix.patterns.registry import _patterns, reset_pattern_registry

    saved = dict(_patterns)
    yield
    reset_pattern_registry()
    _patterns.update(saved)


async def _bound_tools(*, artifacts: bool, object_store: object | None) -> list[str]:
    """The tool names `build_agent` hands the pattern for a manifest with no tools of its own."""
    from felix.config import Settings
    from felix.manifests.builder import BuildDeps, build_agent
    from felix.patterns.registry import register_pattern
    from felix.tools.provider import InMemoryToolProvider

    seen: list[str] = []

    async def capture(ctx: dict) -> object:
        seen.extend(t.name for t in ctx["tools"])
        return object()

    register_pattern("artifact-probe", capture)
    settings = Settings()
    await build_agent(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": "artifact-probe"},
            "spec": {"pattern": "artifact-probe", "artifacts": {"enabled": artifacts}},
        },
        deps=BuildDeps(
            tools=InMemoryToolProvider(), settings=settings, tenant_id="t", object_store=object_store
        ),
        settings=settings,
    )
    return seen


@pytest.mark.asyncio
@pytest.mark.usefixtures("_restore_patterns")
async def test_the_reader_is_bound_only_where_there_is_something_to_read() -> None:
    assert READ_ARTIFACT_TOOL in await _bound_tools(artifacts=True, object_store=_Store())
    # No store: the spill is a no-op, so a reader would offer the model a call that can
    # only ever fail. Enabled-but-storeless is the branch a default deployment could hit.
    assert READ_ARTIFACT_TOOL not in await _bound_tools(artifacts=True, object_store=None)
    assert READ_ARTIFACT_TOOL not in await _bound_tools(artifacts=False, object_store=_Store())
