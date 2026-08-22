"""Unit tests for skills, compaction, session trees, steer, hooks, and SDK shapes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from felix.config import Settings
from felix.hooks import get_agent_hooks, reset_agent_hooks, run_before_turn, run_filter_history
from felix.patterns.types import ChatMessage
from felix.session.compaction import CompactingSessionStrategy, estimate_tokens
from felix.session.store import InMemorySessionStore
from felix.session.strategies import get_session_strategy
from felix.session.tree import annotate_and_append, fork_thread, get_event_id, get_leaf, rewind_to
from felix.session.types import AppendableEvent
from felix.skills.loader import load_manifest_skills, parse_skill_md, skill_catalog_xml
from felix.skills.store import InMemorySkillActivationStore
from felix.skills.tools import make_skill_tools
from felix.steer import enqueue, should_cancel_remaining_tools
from felix.tools.types import ToolInvocationCtx


def test_parse_skill_md() -> None:
    raw = """---
name: demo-skill
description: Does demo things when asked.
---

# Demo

Do the thing.
"""
    skill = parse_skill_md(raw, fallback_name="demo")
    assert skill is not None
    assert skill.name == "demo-skill"
    assert "Do the thing" in skill.body


@pytest.mark.asyncio
async def test_load_bundled_calculator_help() -> None:
    root = Path(__file__).resolve().parents[2] / "skills"
    catalog = await load_manifest_skills(
        [{"name": "calculator-help"}],
        bundled_dir=root,
    )
    assert "calculator-help" in catalog.skills
    xml = skill_catalog_xml(catalog)
    assert "calculator-help" in xml
    assert "<available_skills>" in xml


@pytest.mark.asyncio
async def test_skill_tools_activate() -> None:
    catalog = await load_manifest_skills(
        [{"name": "calculator-help"}],
        bundled_dir=Path(__file__).resolve().parents[2] / "skills",
    )
    store = InMemorySkillActivationStore()
    tools = {
        t.name: t
        for t in make_skill_tools(catalog, activation_store=store, tenant_id="t", manifest_id="quick")
    }
    listed = await tools["list_skills"].executor.execute({}, ToolInvocationCtx())
    data = json.loads(listed if isinstance(listed, str) else listed.content)
    assert any(s["name"] == "calculator-help" for s in data)
    activated = await tools["activate_skill"].executor.execute(
        {"name": "calculator-help"}, ToolInvocationCtx()
    )
    payload = json.loads(activated if isinstance(activated, str) else activated.content)
    assert payload["activated"] == "calculator-help"
    assert "Calculator Help" in payload["instructions"] or "calculator" in payload["instructions"].lower()


@pytest.mark.asyncio
async def test_compacting_strategy_without_model() -> None:
    store = InMemorySessionStore()
    session = store.open("t:compact")
    # Many long events to exceed tiny window
    for i in range(20):
        await annotate_and_append(
            session,
            [
                AppendableEvent(
                    kind="message",
                    role="user",
                    content=("hello world " * 200) + str(i),
                )
            ],
        )
    strategy = CompactingSessionStrategy(
        reserve_tokens=10,
        keep_recent_tokens=50,
        context_window_tokens=100,
        enabled=True,
    )
    out = await strategy.render(
        session,
        [ChatMessage(role="user", content="continue")],
        {"system_prompt": "sys", "model": None},
    )
    assert out[0].role == "system"
    assert any("compaction unavailable" in (m.content or "") for m in out)


def test_get_session_strategy_compacting() -> None:
    s = get_session_strategy("compacting")
    assert isinstance(s, CompactingSessionStrategy)
    s2 = get_session_strategy("summarizing:5")
    assert isinstance(s2, CompactingSessionStrategy)
    assert s2.keep_turns == 5


def test_estimate_tokens() -> None:
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("a" * 40) == 10


@pytest.mark.asyncio
async def test_session_tree_fork_rewind() -> None:
    store = InMemorySessionStore()
    src = store.open("t:src")
    await annotate_and_append(
        src,
        [AppendableEvent(kind="message", role="user", content="A")],
    )
    await annotate_and_append(
        src,
        [AppendableEvent(kind="message", role="assistant", content="B")],
    )
    leaf = get_leaf("t:src")
    assert leaf
    events = await src.get_events()
    first_id = get_event_id(events[0])
    assert first_id

    dest = store.open("t:fork")
    result = await fork_thread(src, dest)
    assert result["ok"]
    assert result["copied"] == 2

    rewound = await rewind_to(src, first_id)
    assert rewound["ok"]
    assert get_leaf("t:src") == first_id


@pytest.mark.asyncio
async def test_steer_enqueue() -> None:
    out = await enqueue("default", "default:run1", kind="steer", text="stop")
    assert out["queued"] == "steer"
    assert await should_cancel_remaining_tools("default", "default:run1")


@pytest.mark.asyncio
async def test_plugin_hooks() -> None:
    reset_agent_hooks()
    hooks = get_agent_hooks()

    async def inject(_msgs, _ctx):
        return [ChatMessage(role="system", content="injected")]

    async def filter_hist(history, _ctx):
        return [
            m for m in history if getattr(m, "role", None) != "system" or "keep" in getattr(m, "content", "")
        ]

    hooks.register_before_turn(inject)
    hooks.register_filter_history(filter_hist)

    injected = await run_before_turn([])
    assert injected and injected[0].content == "injected"
    filtered = await run_filter_history(
        [
            ChatMessage(role="system", content="drop me"),
            ChatMessage(role="system", content="keep me"),
            ChatMessage(role="user", content="hi"),
        ]
    )
    assert len(filtered) == 2
    reset_agent_hooks()


@pytest.mark.asyncio
async def test_build_agent_with_skills() -> None:
    from felix.manifests.builder import BuildDeps, build_agent
    from felix.tools.builtins import default_tool_provider

    settings = Settings(database_url="memory://test", object_store="memory", allow_insecure=True)
    agent = await build_agent(
        "quick",
        deps=BuildDeps(
            tools=default_tool_provider(),
            settings=settings,
            tenant_id="default",
        ),
        settings=settings,
    )
    assert "list_skills" in {t.name for t in agent.tools}
    assert "calculator-help" in agent.system_prompt or "available_skills" in agent.system_prompt


def test_felix_client_import() -> None:
    from felix.sdk import FelixClient

    c = FelixClient(base_url="http://localhost:8080")
    c.set_model("claude-haiku-4")
    c.set_thread("abc")
    assert c._model == "claude-haiku-4"


@pytest.mark.asyncio
async def test_context_files_from_store() -> None:
    from felix.context_files import load_instruction_files
    from felix.storage import MemoryObjectStore

    store = MemoryObjectStore()
    await store.put("AGENTS.md", b"# Project\nUse tabs.")
    parts = await load_instruction_files(file_keys=["AGENTS.md"], object_store=store, tenant_id="t")
    assert parts and "Use tabs" in parts[0]


@pytest.mark.asyncio
async def test_model_change_event_kind() -> None:
    store = InMemorySessionStore()
    session = store.open("t:model")
    await annotate_and_append(
        session,
        [
            AppendableEvent(
                kind="model_change",
                content="claude-haiku-4",
                metadata={"type": "model_change", "model_id": "claude-haiku-4"},
            )
        ],
    )
    events = await session.get_events()
    assert events[0].kind == "model_change"
    assert get_event_id(events[0])
