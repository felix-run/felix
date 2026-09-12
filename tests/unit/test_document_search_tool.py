"""The `search_documents` tool: `spec.document_tools` → a tool an agent can call.

The corpus landed before the tool did — ingestion, a hybrid store, both backends and the
`/documents` management routes — so an operator could fill it and no agent could read it.
This is the half that closes that, and the roadmap's rule is the reason it matters: *a tool
no manifest declares is inert*, and until now the support agent could fetch a page it already
knew the URL of and could not search for one.

What is worth pinning here is the wiring rather than the retrieval, which the store's own
conformance arm covers against both backends: that the manifest field binds a tool at all,
that the tool reaches the calling tenant's corpus and no other, that governance treats what
comes back as untrusted, and that the two empty answers stay distinguishable.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.config import Settings
from felix.documents import store as documents
from felix.manifests.schema import DocumentSearchToolRef
from felix.tools.document_search import EMPTY_CORPUS, tools_from_document_refs

TENANT = "acme"


def _settings() -> Settings:
    return Settings(database_url="memory://documents-tool", object_store="memory")


async def _ingest(settings: Settings, tenant: str, title: str, body: str) -> None:
    await documents.put_document(
        settings, tenant_id=tenant, title=title, source=f"https://docs.example/{title}", text=body
    )


def _tool(settings: Settings, tenant: str = TENANT, **kw: Any) -> Any:
    ref = DocumentSearchToolRef(name="search_docs", **kw)
    tools = tools_from_document_refs([ref], settings=settings, tenant_id=tenant)
    assert len(tools) == 1
    return tools[0]


@pytest.mark.asyncio
async def test_the_tool_returns_a_chunk_an_operator_ingested() -> None:
    settings = _settings()
    await _ingest(settings, TENANT, "deploy", "Set FELIX_OBJECT_STORE to fs for a local run.")

    out = await _tool(settings).executor.execute({"query": "object store"})

    assert "FELIX_OBJECT_STORE" in out, out
    assert "deploy" in out, out


@pytest.mark.asyncio
async def test_the_corpus_is_the_calling_tenants_own() -> None:
    """The tenant comes from the binding, not from the call, so a model cannot ask for another.

    This is the one thing the tool adds over the store it wraps: the store takes a tenant, and
    something has to decide which. Getting it from the compile is what makes the answer safe.
    """
    settings = _settings()
    await _ingest(settings, TENANT, "ours", "The acme runbook lives here.")
    await _ingest(settings, "other-tenant", "theirs", "The other runbook lives here.")

    out = await _tool(settings).executor.execute({"query": "runbook"})

    assert "acme runbook" in out, out
    assert "other runbook" not in out, out


@pytest.mark.asyncio
async def test_an_empty_corpus_says_so_rather_than_reporting_a_miss() -> None:
    """ "Nothing ingested" and "nothing matched" lead somewhere different.

    A model told the second rephrases and retries; told the first it stops and says the corpus
    is empty, which is the answer an operator can act on.
    """
    settings = _settings()

    assert await _tool(settings).executor.execute({"query": "anything"}) == EMPTY_CORPUS

    await _ingest(settings, TENANT, "one", "Something unrelated entirely.")
    out = await _tool(settings).executor.execute({"query": "zzzz-no-such-token"})
    assert out != EMPTY_CORPUS
    assert "No documents matched." in out, out


@pytest.mark.asyncio
async def test_the_result_cap_is_the_manifests(monkeypatch: pytest.MonkeyPatch) -> None:
    """`max_results` is the whole cost of this tool in a context window."""
    settings = _settings()
    for i in range(6):
        await _ingest(settings, TENANT, f"page-{i}", f"Retrieval note number {i} about widgets.")

    out = await _tool(settings, max_results=2).executor.execute({"query": "widgets"})

    assert out.count("https://docs.example/") <= 2, out


@pytest.mark.asyncio
async def test_a_store_failure_names_the_class_and_not_the_detail() -> None:
    """A store error can carry a table or a connection string; the model gets neither."""
    settings = _settings()
    tool = _tool(settings)

    async def _boom(*_a: Any, **_kw: Any) -> Any:
        raise RuntimeError("relation felix_documents does not exist at host db.internal")

    import felix.documents.store as store_module

    original = store_module.search_documents
    store_module.search_documents = _boom  # type: ignore[assignment]
    try:
        out = await tool.executor.execute({"query": "anything"})
    finally:
        store_module.search_documents = original  # type: ignore[assignment]

    assert out == "document_search_error: RuntimeError"
    assert "felix_documents" not in out
    assert "db.internal" not in out


def test_the_transport_is_untrusted_so_screening_covers_it() -> None:
    """Governance decides trust by an allowlist, and this is not on it.

    The tool reaches no network, which is the argument for *not* needing an egress guard —
    and is not an argument about the content. A chunk is text somebody else wrote.
    """
    from felix.manifests.builder import _is_untrusted_tool

    tool = _tool(_settings())

    assert tool.executor.transport == "documents"
    assert _is_untrusted_tool(tool) is True


@pytest.mark.asyncio
async def test_support_can_look_something_up() -> None:
    """The finding this whole workstream started from, asserted on the compiled agent.

    The audit's words were "a support agent that cannot look anything up": `support.yaml`
    declared `tools: [calculator, list_skills]`. `fetch_docs` gave it a page it already knew
    the URL of; this gives it the question an operator actually asks, which is where something
    is written down.

    Through `build_agent` rather than the binder, because a field that binds a tool the
    compile then drops is the inert-field shape this repo keeps finding.
    """
    from felix.manifests.builder import build_agent
    from felix.tools.builtins import default_tool_provider

    agent = await build_agent("support", default_tool_provider(), settings=_settings())
    names = {t.name for t in agent.tools}

    assert {"search_docs", "fetch_docs"} <= names, f"support still cannot look anything up: {sorted(names)}"


@pytest.mark.asyncio
async def test_a_manifest_binding_it_without_screening_is_warned(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The operator is told, at compile, that retrieved text reaches the model unscreened.

    Screening is a wrapper, and the wrapper keeps the transport — so "was it applied" is not
    readable off the compiled tool. What *is* observable is the warning the builder emits for
    an untrusted tool with screening off, and that warning naming `search_docs` is the same
    evidence: governance classified it as untrusted and said so.

    `support.yaml` enables screening precisely so this stays silent for what we ship.
    """
    import logging

    from felix.manifests.builder import build_agent
    from felix.manifests.loader import load_bundled
    from felix.tools.builtins import default_tool_provider

    manifest = load_bundled("support")
    manifest.spec.content_screening.enabled = False

    with caplog.at_level(logging.WARNING, logger="felix.manifests.builder"):
        await build_agent(manifest, default_tool_provider(), settings=_settings())

    unscreened = [r.getMessage() for r in caplog.records if "unscreened" in r.getMessage()]
    assert unscreened, "binding a documents tool with screening off said nothing"
    assert "search_docs" in unscreened[0], unscreened[0]


@pytest.mark.asyncio
async def test_the_bundled_support_manifest_stays_silent() -> None:
    """The bar for shipping that warning: it must not fire on what we ship."""
    import logging

    from felix.manifests.builder import build_agent
    from felix.tools.builtins import default_tool_provider

    logger = logging.getLogger("felix.manifests.builder")
    records: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    handler = _Capture(level=logging.WARNING)
    logger.addHandler(handler)
    try:
        await build_agent("support", default_tool_provider(), settings=_settings())
    finally:
        logger.removeHandler(handler)

    assert not [m for m in records if "unscreened" in m], records
