"""Unit tests for second-pass session/loop surfaces."""

from __future__ import annotations

import pytest
from felix.session.types import SessionEvent, analyze_wake, include_in_llm_context


def _ev(
    seq: int,
    *,
    kind: str = "message",
    role: str | None = "user",
    content: str | None = "",
    metadata: dict | None = None,
    tool_calls: list | None = None,
    tool_call_id: str | None = None,
) -> SessionEvent:
    return SessionEvent(
        seq=seq,
        ts=float(seq),
        kind=kind,  # type: ignore[arg-type]
        role=role,  # type: ignore[arg-type]
        content=content,
        metadata=metadata,
        tool_calls=tool_calls,
        tool_call_id=tool_call_id,
    )


def test_thinking_level_budgets() -> None:
    from felix.session.thinking import THINKING_LEVELS, budget_for_level, parse_thinking_level

    assert parse_thinking_level("high") == "high"
    assert budget_for_level("off") is None
    assert budget_for_level("medium") == 1024
    assert budget_for_level("max") == 32000
    assert len(THINKING_LEVELS) == 7
    with pytest.raises(ValueError):
        parse_thinking_level("nope")


def test_apply_thinking_to_spec() -> None:
    from felix.manifests.schema import ModelSpec
    from felix.session.thinking import apply_thinking_to_spec

    spec = ModelSpec(id="claude-sonnet", thinking_budget=None)
    updated = apply_thinking_to_spec(spec, "low")
    assert updated.thinking_budget == 512
    assert updated.thinking_level == "low"


def test_estimate_cost_and_usage() -> None:
    from felix.usage.pricing import estimate_cost, usage_with_cost

    cost = estimate_cost(model_id="claude-sonnet", tokens_input=1_000_000, tokens_output=0)
    assert cost["input"] == 3.0
    assert cost["total"] == 3.0
    block = usage_with_cost(
        {"input": 1000, "output": 500, "cache_read": 0, "cache_creation": 0},
        model_id="gpt-4o-mini",
    )
    assert block["input"] == 1000
    assert block["output"] == 500
    assert "cost" in block
    assert block["cost"]["total"] > 0


def test_include_in_llm_context_custom() -> None:
    plain = _ev(0, kind="custom", role="assistant", content="ui-only")
    assert not include_in_llm_context(plain)
    in_ctx = _ev(1, kind="custom", role="user", content="plugin note", metadata={"in_context": True})
    assert include_in_llm_context(in_ctx)
    assert include_in_llm_context(_ev(2, kind="message", role="user", content="hi"))
    assert not include_in_llm_context(_ev(3, kind="thinking_level_change", role="system", content="high"))


def test_build_snapshot_resolves_thinking() -> None:
    from felix.session.snapshot import build_snapshot

    events = [
        _ev(0, role="user", content="hi"),
        _ev(
            1,
            kind="thinking_level_change",
            role="system",
            content="high",
            metadata={"type": "thinking_level_change", "thinking_level": "high"},
        ),
        _ev(2, role="assistant", content="yo"),
    ]
    snap = build_snapshot(thread_id="t1:s", events=events, phase="idle")
    assert snap["thinkingLevel"] == "high"
    assert snap["phase"] == "idle"
    assert len(snap["transcript"]) == 3


def test_abandoned_events_after_common_ancestor() -> None:
    from felix.session.branch import abandoned_events

    a = _ev(0, content="a", metadata={"event_id": "a"})
    b = _ev(1, role="assistant", content="b", metadata={"event_id": "b", "parent_id": "a"})
    c = _ev(2, content="c", metadata={"event_id": "c", "parent_id": "b"})
    abandoned = abandoned_events([a, b, c], old_leaf_id="c", new_leaf_id="b", session_id="s")
    ids = [(e.metadata or {}).get("event_id") for e in abandoned]
    assert ids == ["c"]


@pytest.mark.asyncio
async def test_summarize_abandoned_branch_fallback() -> None:
    from felix.session.branch import summarize_abandoned_branch
    from felix.session.store import InMemorySessionStore
    from felix.session.tree import annotate_and_append
    from felix.session.types import AppendableEvent

    store = InMemorySessionStore()
    session = store.open("tenant:thread-branch")
    ids = await annotate_and_append(
        session,
        [
            AppendableEvent(kind="message", role="user", content="start"),
            AppendableEvent(kind="message", role="assistant", content="gone"),
        ],
    )
    result = await summarize_abandoned_branch(
        session,
        old_leaf_id=ids[1],
        new_leaf_id=ids[0],
        model=None,
    )
    assert result is not None
    assert result["ok"] is True
    assert "Abandoned branch" in (result["summary"] or "")


def test_compaction_cut_never_on_tool_result() -> None:
    from felix.session.compaction import _find_cut

    events = [
        _ev(0, content="q" * 40),
        _ev(
            1,
            role="assistant",
            content="",
            tool_calls=[{"id": "1", "name": "calc", "args": {}}],
        ),
        _ev(2, kind="tool_result", role="tool", content="42", tool_call_id="1"),
        _ev(3, role="assistant", content="done" * 20),
        _ev(4, content="next" * 20),
    ]
    _older, kept, _ = _find_cut(events, keep_recent_tokens=30, keep_turns=None)
    if kept:
        assert kept[0].kind != "tool_result"
        assert kept[0].role != "tool"


def test_serialize_conversation_shapes() -> None:
    from felix.session.compaction import serialize_conversation

    text = serialize_conversation(
        [
            _ev(0, content="hello"),
            _ev(
                1,
                role="assistant",
                content="",
                tool_calls=[{"id": "1", "name": "calc", "args": {"x": 1}}],
            ),
            _ev(2, kind="tool_result", role="tool", content="ok", tool_call_id="1"),
        ]
    )
    assert "[User]: hello" in text
    assert "[Assistant tool calls]" in text
    assert "[Tool result]" in text


@pytest.mark.asyncio
async def test_search_sessions_memory() -> None:
    from felix.config import Settings
    from felix.session.search import (
        index_event_memory,
        reset_search_index_for_tests,
        search_sessions,
    )

    reset_search_index_for_tests()
    settings = Settings(
        allow_insecure=True,
        auth_mode="none",
        environment="development",
        database_url="memory://search",
        object_store="memory",
    )
    index_event_memory(
        tenant_id="default",
        thread_id="t1",
        seq=1,
        content="the quick brown fox",
        event_id="e1",
    )
    hits = await search_sessions(settings, "default", "brown", limit=10)
    assert len(hits) == 1
    assert hits[0]["thread_id"] == "t1"


def test_eval_pass_rate_lift() -> None:
    from felix.eval.compare import EvalHarness, eval_harness_table, pass_rate, pass_rate_lift

    assert pass_rate([{"pass": True}, {"pass": False}]) == 0.5
    assert pass_rate_lift([{"pass": True}], [{"pass": True}, {"pass": True}]) == 0.0
    assert pass_rate_lift([{"pass": False}], [{"pass": True}]) == 100.0
    rows = eval_harness_table(
        "smoke",
        baseline=EvalHarness(name="base", manifest="quick"),
        candidates=[EvalHarness(name="cand", manifest="quick", repetitions=2)],
        repetitions=1,
    )
    assert len(rows) == 3


def test_react_batch_mode_parallel_vs_sequential() -> None:
    from felix.patterns.react import _ReactAgent
    from felix.patterns.types import ToolCall
    from felix.tools.types import define_tool

    async def _ok(_a=None, _c=None):
        return "ok"

    local = define_tool(name="a", description="", handler=_ok, transport="local")
    client = define_tool(name="b", description="", handler=_ok, transport="client")
    agent = _ReactAgent(
        tools=[local, client],
        pattern="react",
        manifest_id="t",
        manifest_version="1",
        system_prompt="",
        model_spec=None,
        settings=None,
        recursion_limit=5,
        tool_execution="parallel",
    )
    assert (
        agent._batch_mode([ToolCall(id="1", name="a", args={}), ToolCall(id="2", name="a", args={})])
        == "parallel"
    )
    assert (
        agent._batch_mode([ToolCall(id="1", name="a", args={}), ToolCall(id="2", name="b", args={})])
        == "sequential"
    )


@pytest.mark.asyncio
async def test_steer_abort_and_drain_modes() -> None:
    from felix.steer import (
        clear_abort,
        drain_steer,
        enqueue,
        is_aborted,
        request_abort,
    )

    tid = "unit-abort-thread"
    await clear_abort("default", tid)
    await enqueue("default", tid, kind="steer", text="steer me")
    await enqueue("default", tid, kind="steer", text="again")
    one = await drain_steer("default", tid, mode="one-at-a-time")
    assert len(one) == 1
    rest = await drain_steer("default", tid, mode="all")
    assert len(rest) == 1
    r = await request_abort("default", tid)
    assert r["aborted"] is True
    assert await is_aborted("default", tid)
    await clear_abort("default", tid)
    assert not await is_aborted("default", tid)


def test_analyze_wake_skips_meta_events() -> None:
    events = [
        _ev(0, content="hi"),
        _ev(
            1,
            kind="thinking_level_change",
            role="system",
            content="high",
            metadata={"thinking_level": "high"},
        ),
        _ev(2, role="assistant", content="hello"),
    ]
    wake = analyze_wake(events)
    assert wake.ended_on_assistant is True
    assert wake.fresh is False


def test_expand_template_placeholders() -> None:
    from felix.prompts import expand_template

    assert expand_template("Hello $1", ["World"]) == "Hello World"
    assert expand_template("All: $@", ["a", "b"]) == "All: a b"
    assert expand_template("X ${1:-fallback}", []) == "X fallback"
    assert expand_template("X ${1:-fallback}", ["ok"]) == "X ok"
    assert expand_template("$2-$1", ["a", "b"]) == "b-a"


@pytest.mark.asyncio
async def test_expand_named_prompt_from_manifest() -> None:
    from felix.manifests.schema import Manifest, Metadata, PromptTemplateSpec, Spec
    from felix.prompts import expand_named_prompt

    m = Manifest(
        metadata=Metadata(name="quick"),
        spec=Spec(
            prompts=[
                PromptTemplateSpec(
                    name="review",
                    body="Review $1 with focus on ${2:-security}",
                )
            ]
        ),
    )
    out = await expand_named_prompt(m, "review", ["auth.py"])
    assert out == "Review auth.py with focus on security"
    out2 = await expand_named_prompt(m, "review", ["auth.py", "perf"])
    assert out2 == "Review auth.py with focus on perf"


def test_events_to_jsonl_export() -> None:
    from felix.session.export import events_to_jsonl

    text = events_to_jsonl(
        [
            _ev(0, content="hi", metadata={"event_id": "a"}),
            _ev(1, role="assistant", content="yo", metadata={"event_id": "b", "parent_id": "a"}),
        ]
    )
    lines = [ln for ln in text.strip().split("\n") if ln]
    assert len(lines) == 2
    assert '"event_id": "a"' in lines[0]


def test_model_catalog_entry_shape() -> None:
    from felix.usage.catalog import catalog_from_manifest, model_catalog_entry

    entry = model_catalog_entry(model_id="claude-sonnet-4")
    assert entry["object"] == "model"
    assert entry["felix"]["contextWindow"] >= 100_000
    assert "off" in entry["felix"]["supportedThinkingLevels"]
    assert entry["felix"]["cost"]["inputPerMillion"] > 0
    listed = catalog_from_manifest("quick", None)
    assert listed["id"] == "quick"


@pytest.mark.asyncio
async def test_session_lease_exclusive() -> None:
    from felix.session.lease import acquire_lease, release_lease, reset_leases_for_tests

    reset_leases_for_tests()
    a = await acquire_lease("t:1", holder_id="tab-a", mode="exclusive")
    assert a["ok"] is True
    assert a["status"]["locked"] is True
    b = await acquire_lease("t:1", holder_id="tab-b", mode="exclusive")
    assert b["ok"] is False
    await release_lease("t:1", holder_id="tab-a", token=a["token"])
    c = await acquire_lease("t:1", holder_id="tab-b", mode="exclusive")
    assert c["ok"] is True


def test_provider_handoff_detection() -> None:
    from felix.patterns.types import ChatMessage
    from felix.session.handoff import handoff_system_message, needs_handoff

    assert needs_handoff("claude-sonnet-4", "gpt-4.1")
    assert not needs_handoff("claude-sonnet-4", "claude-haiku-4")
    note = handoff_system_message(
        [ChatMessage(role="user", content="hi")],
        previous_model="claude-sonnet-4",
        next_model="gpt-4.1",
    )
    assert note is not None
    assert "model handoff" in note.content


def test_multimodal_chat_message_parse() -> None:
    from felix.patterns.model import _messages_to_openai
    from felix.patterns.types import ChatMessage

    msg = ChatMessage.model_validate(
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "what is this?"},
                {
                    "type": "image_url",
                    "image_url": {"url": "https://example.com/x.png", "detail": "low"},
                },
            ],
        }
    )
    assert msg.content == "what is this?"
    assert msg.attachments and msg.attachments[0].url.endswith("x.png")
    oai = _messages_to_openai([msg])
    assert isinstance(oai[0]["content"], list)
    assert oai[0]["content"][1]["type"] == "image_url"


@pytest.mark.asyncio
async def test_ui_resolve_smoke() -> None:
    from felix.ui import resolve_ui_response

    out = await resolve_ui_response("noop-id", value="ok")
    assert out["ok"] is True
