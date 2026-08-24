"""The memory tools, and where they sit relative to the governance stack.

The placement is the point. An automatic fact prelude reaches the model without
passing through a single wrapper; a tool bound before the governance block passes
through all nine. Recalled text is model-extracted from earlier turns and can carry
whatever a tool returned, so the difference is not cosmetic.
"""

from __future__ import annotations

import pytest
from felix.config import Settings
from felix.manifests.schema import Manifest
from felix.memory import store as memory_store
from felix.memory.tools import MEMORY_TOOL_NAMES, make_memory_tools

TENANT = "t-tools"
MANIFEST = "m"


@pytest.fixture(autouse=True)
def _clean() -> None:
    memory_store._memory_rows.clear()


def _settings() -> Settings:
    return Settings(database_url="memory://tools", object_store="memory", allow_insecure=True)


def _tools() -> dict[str, object]:
    return {
        t.name: t
        for t in make_memory_tools(
            settings=_settings(), tenant_id=TENANT, manifest_id=MANIFEST, thread_id="th-1"
        )
    }


async def _run(name: str, **args: object) -> str:
    tool = _tools()[name]
    return await tool.executor.execute(args)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_remember_then_recall_round_trip() -> None:
    out = await _run("remember", content="The deploy runbook lives in the ops repo.")
    assert out.startswith("remembered:")

    recalled = await _run("recall", query="where is the runbook")
    assert "ops repo" in recalled


@pytest.mark.asyncio
async def test_recall_reports_no_hits_rather_than_erroring() -> None:
    assert "no relevant memories" in await _run("recall", query="something never stored")


@pytest.mark.asyncio
async def test_topic_key_supersedes_through_the_tool() -> None:
    """The convention the tool description teaches has to actually work."""
    await _run("remember", content="Timezone is UTC.", topic_key="user.timezone")
    await _run("remember", content="Timezone is CET.", topic_key="user.timezone")

    listed = await _run("list_memories")
    assert "CET" in listed
    assert "UTC" not in listed


@pytest.mark.asyncio
async def test_forget_removes_from_recall() -> None:
    stored = await _run("remember", content="A regrettable detail about the runbook.")
    mem_id = stored.split(":", 1)[1]

    assert await _run("forget", id=mem_id) == f"forgot:{mem_id}"
    assert "no relevant memories" in await _run("recall", query="regrettable runbook")


@pytest.mark.asyncio
async def test_forget_is_honest_about_a_miss() -> None:
    assert "no such memory" in await _run("forget", id="not-a-real-id")


@pytest.mark.asyncio
async def test_an_unknown_kind_falls_back_rather_than_failing() -> None:
    """The model supplies `kind`; a bad value must not fail the turn."""
    await _run("remember", content="Something.", kind="nonsense")
    rows = await memory_store.list_active(_settings(), TENANT, manifest_id=MANIFEST)
    assert rows[0]["kind"] == "fact"


@pytest.mark.asyncio
async def test_remember_records_the_thread_it_came_from() -> None:
    await _run("remember", content="Learned right here.")
    rows = await memory_store.list_active(_settings(), TENANT, manifest_id=MANIFEST)
    assert rows[0]["thread_id"] == "th-1"


def test_tool_descriptions_teach_the_supersession_convention() -> None:
    """A convention the model is never told about is a convention it will not follow."""
    remember = _tools()["remember"]
    assert "topic_key" in remember.description
    assert "supersede" in remember.description.lower()


# --- governance placement ---------------------------------------------------------


def _manifest(**recall: object) -> Manifest:
    return Manifest.model_validate(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": MANIFEST},
            "spec": {"memory": {"store": "memory", "recall": recall}},
        }
    )


def test_memory_tools_are_off_by_default() -> None:
    assert _manifest().spec.memory.recall.tools is False


# Names the governance wrappers tag their refusals with. A memory tool bound *below*
# the wrapper block would answer normally instead of being tagged by one of these.
_GOVERNANCE_SOURCES = {
    "secrets",
    "policies",
    "command_screening",
    "content_screening",
    "limits",
    "guardrails",
    "judges",
    "approvals",
    "artifacts",
}


@pytest.mark.asyncio
async def test_bound_tools_pass_through_the_governance_stack() -> None:
    """Every memory tool is wrapped, asserted by behaviour rather than by structure.

    This is the whole argument for the tool path over the automatic fact prelude: if
    the binding moved below the governance block in `builder.py`, these tools would
    still work and nothing would screen their output.

    With no request context there is no budget, so the `limits` wrapper refuses first
    and the refusal carries its source. Which wrapper wins is not the point — that one
    of them speaks at all is.
    """
    from felix.manifests.builder import BuildDeps, build_agent
    from felix.tools.provider import InMemoryToolProvider

    manifest = Manifest.model_validate(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": MANIFEST},
            "spec": {
                "memory": {"store": "memory", "recall": {"tools": True}},
                "policies": [{"id": "gated", "required_scopes": ["memory:recall"], "tools": ["recall"]}],
            },
        }
    )
    agent = await build_agent(
        manifest,
        deps=BuildDeps(tools=InMemoryToolProvider(), settings=_settings(), tenant_id=TENANT),
    )
    bound = {t.name: t for t in agent.tools}
    assert set(MEMORY_TOOL_NAMES) <= set(bound), "memory tools were not bound at all"

    for name in MEMORY_TOOL_NAMES:
        args = {"query": "anything"} if name == "recall" else {}
        if name == "remember":
            args = {"content": "anything"}
        if name == "forget":
            args = {"id": "x"}
        out = await bound[name].executor.execute(args)
        source = (getattr(out, "metadata", None) or {}).get("source")
        assert source in _GOVERNANCE_SOURCES, (
            f"{name} was not wrapped by the governance stack — is it still bound "
            f"before the wrapper block in builder.py? got source={source!r}"
        )
