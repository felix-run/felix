"""Characterization of `CompactingSessionStrategy.render()`: what it does today, branch by branch.

These pin the *current* output of the paths the behavioural suites did not reach -- the exact
messages rendered (role, content, tool_call_id, tool_calls) and the checkpoint persisted -- so a
restructuring of `render()` that changes any of it fails here. Several pinned behaviours are odd;
they are pinned as they are, each marked `CURRENT BEHAVIOUR`, not endorsed. A change to one of
them is a behaviour change and belongs in its own commit with its own test.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.hooks import get_agent_hooks, reset_agent_hooks
from felix.patterns.model import ModelChatResult
from felix.patterns.types import ChatMessage
from felix.session.compaction import (
    _UNTRUSTED_NOTICE,
    STRUCTURED_SUMMARY_PROMPT,
    TURN_PREFIX_PROMPT,
    CompactingSessionStrategy,
    fence_untrusted,
    summary_message,
    turn_prefix_message,
)
from felix.session.store import InMemorySessionStore
from felix.session.tree import annotate_and_append, rewind_to
from felix.session.types import AppendableEvent, SessionRenderOpts

INCOMING = [ChatMessage(role="user", content="next")]
SYS = ("system", "sys", None, None)
NEXT = ("user", "next", None, None)
NO_FILES = {"readFiles": [], "modifiedFiles": []}


class _Model:
    """Answers each summariser by its prompt; records every call it gets."""

    def __init__(self, *, history: str = "HIST", prefix: str = "PFX", fail: bool = False) -> None:
        self.history_text = history
        self.prefix_text = prefix
        self.fail = fail
        self.calls: list[list[tuple[str, str | None]]] = []

    async def chat(self, messages: list[ChatMessage], tools: Any, opts: Any = None) -> ModelChatResult:
        self.calls.append([(m.role, m.content) for m in messages])
        if self.fail:
            raise RuntimeError("provider down")
        is_prefix = (messages[0].content or "").startswith(TURN_PREFIX_PROMPT)
        text = self.prefix_text if is_prefix else self.history_text
        return ModelChatResult(message=ChatMessage(role="assistant", content=text), stop_reason="end_turn")


@pytest.fixture(autouse=True)
def _metered(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every summariser call is metered; the usage block names the kind it was metered under."""
    import felix.patterns.model as model_mod

    monkeypatch.setattr(
        model_mod, "record_model_usage", lambda result, model, **kw: {"metered": kw["meta"]["kind"]}
    )


@pytest.fixture(autouse=True)
def _hooks():
    reset_agent_hooks()
    yield
    reset_agent_hooks()


def _shape(out: list[ChatMessage]) -> list[tuple[Any, ...]]:
    return [
        (
            m.role,
            m.content,
            m.tool_call_id,
            [(tc.id, tc.name, tc.args) for tc in m.tool_calls] if m.tool_calls else None,
        )
        for m in out
    ]


def _u(content: str, **kw: Any) -> AppendableEvent:
    return AppendableEvent(kind="message", role="user", content=content, **kw)


def _a(content: str, **kw: Any) -> AppendableEvent:
    return AppendableEvent(kind="message", role="assistant", content=content, **kw)


def _call(i: int) -> AppendableEvent:
    return _a("", tool_calls=[{"id": f"c{i}", "name": "read_file", "args": {"path": f"f{i}"}}])


def _body(i: int) -> str:
    return f"r{i} " + "x" * 80


def _result(i: int) -> AppendableEvent:
    return AppendableEvent(
        kind="tool_result", role="tool", content=_body(i), tool_call_id=f"c{i}", name="read_file"
    )


def _steps(*indices: int) -> list[AppendableEvent]:
    return [e for i in indices for e in (_call(i), _result(i))]


def _call_msg(i: int) -> tuple[Any, ...]:
    return ("assistant", "", None, [(f"c{i}", "read_file", {"path": f"f{i}"})])


def _result_msg(i: int) -> tuple[Any, ...]:
    return ("tool", _body(i), f"c{i}", None)


def _item(
    role: str,
    content: str,
    *,
    tool_call_id: str | None = None,
    name: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "role": role,
        "content": content,
        "tool_call_id": tool_call_id,
        "name": name,
        "tool_calls": tool_calls,
        "metadata": {},
    }


def _call_item(i: int) -> dict[str, Any]:
    return _item(
        "assistant", "", tool_calls=[{"id": f"c{i}", "name": "read_file", "args": {"path": f"f{i}"}}]
    )


def _result_item(i: int) -> dict[str, Any]:
    return _item("tool", _body(i), tool_call_id=f"c{i}", name="read_file")


def _summary(text: str) -> tuple[Any, ...]:
    m = summary_message(text)
    return (m.role, m.content, None, None)


def _prefix(text: str) -> tuple[Any, ...]:
    m = turn_prefix_message(text)
    return (m.role, m.content, None, None)


def _roomy(keep: int = 400, **kw: Any) -> CompactingSessionStrategy:
    return CompactingSessionStrategy(
        reserve_tokens=10, keep_recent_tokens=keep, context_window_tokens=10_000_000, **kw
    )


async def _checkpoints(session: Any) -> list[Any]:
    return [e for e in await session.get_events() if e.kind == "compaction"]


async def _forced(session: Any, model: Any, keep: int, **opts: Any) -> list[ChatMessage]:
    return await _roomy(keep).render(
        session, INCOMING, {"system_prompt": "sys", "model": model, "force_compact": True, **opts}
    )


async def _session(thread: str, events: list[AppendableEvent]) -> tuple[Any, list[str]]:
    session = InMemorySessionStore(tenant_id="acme").open(thread)
    ids = await annotate_and_append(session, events)
    return session, ids


async def _with_old_checkpoint(thread: str) -> tuple[Any, list[str]]:
    """q0 a0 | q1 a1 compacted to `OLD` (keeping a1, with q1 as its turn's lead), then q2 a2."""
    session, ids = await _session(thread, [_u("q0"), _a("a0"), _u("q1"), _a("a1")])
    await _roomy(1).compact_now(session, model=_Model(history="OLD"))
    ids += await annotate_and_append(session, [_u("q2"), _a("a2")])
    return session, ids


# -- the render options object, and `keep_turns` -----------------------------------------------


async def test_keep_turns_counts_events_and_forces_a_compaction_inside_the_window() -> None:
    # CURRENT BEHAVIOUR: `keep_turns` caps kept *events*, not turns -- 2 here keeps a1 and q2,
    # and the cut then lands on an assistant step, so it reads as a split turn.
    seen: list[dict[str, Any]] = []
    get_agent_hooks().register_before_compact(lambda prep, ctx: seen.append(prep) and None)
    session, ids = await _session(
        "acme:char-keep-turns", [_u(f"m{i}") if i % 2 == 0 else _a(f"m{i}") for i in range(5)]
    )
    model = _Model()

    out = await _roomy(keep_turns=2).render(
        session, INCOMING, SessionRenderOpts(system_prompt="sys", model=model)
    )

    assert _shape(out) == [
        SYS,
        _summary("HIST"),
        ("user", "m2", None, None),
        ("assistant", "m3", None, None),
        ("user", "m4", None, None),
        NEXT,
    ]
    (prep,) = seen
    assert {k: v for k, v in prep.items() if k != "messages_to_summarize"} == {
        "previous_summary": None,
        "tokens_before": 7,
        "first_kept_entry_id": ids[3],
        "first_kept_seq": 3,
        "is_split_turn": True,
        "file_ops": NO_FILES,
        "reason": "threshold",
        "will_retry": False,
        "custom_instructions": None,
    }
    assert [e.content for e in prep["messages_to_summarize"]] == ["m0", "m1", "m2"]
    (checkpoint,) = await _checkpoints(session)
    assert checkpoint.content == "HIST"
    assert checkpoint.metadata == {
        "type": "compaction",
        "covers_to_seq": 2,
        "first_kept_seq": 3,
        "first_kept_entry_id": ids[3],
        "last_kept_entry_id": ids[4],
        "tokens_before": 7,
        "retainedTail": [_item("user", "m2"), _item("assistant", "m3"), _item("user", "m4")],
        "details": NO_FILES,
        "is_split_turn": True,
        "reason": "threshold",
        "split_turn": {"opening_kept": True, "prefix_summarized": False, "lead_items": 1},
        "usage": {"metered": "compaction"},
    }
    assert model.calls == [
        [
            ("system", STRUCTURED_SUMMARY_PROMPT + _UNTRUSTED_NOTICE),
            ("user", fence_untrusted("[User]: m0\n[Assistant]: m1")),
        ]
    ]


async def test_compact_now_forces_a_manual_pass_and_reports_the_message_count() -> None:
    seen: list[dict[str, Any]] = []
    get_agent_hooks().register_before_compact(lambda prep, ctx: seen.append({**prep, "ctx": ctx}) and None)
    session, ids = await _session("acme:char-compact-now", [_u("q0"), _a("a0"), _u("q1"), _a("a1")])
    model = _Model()

    result = await _roomy(1).compact_now(session, model=model, system_prompt="sys", instructions="FOCUS")

    assert result == {"ok": True, "messages": 4, "reason": "manual"}
    (prep,) = seen
    assert prep["reason"] == "manual"
    assert prep["custom_instructions"] == "FOCUS"
    assert prep["will_retry"] is False
    assert prep["ctx"] == {"session_id": "acme:char-compact-now"}
    assert model.calls[0][0] == ("system", STRUCTURED_SUMMARY_PROMPT + _UNTRUSTED_NOTICE + "\nFocus: FOCUS")
    (checkpoint,) = await _checkpoints(session)
    assert checkpoint.metadata["reason"] == "manual"
    assert checkpoint.metadata["retainedTail"] == [_item("user", "q1"), _item("assistant", "a1")]
    assert checkpoint.metadata["first_kept_entry_id"] == ids[3]


# -- the before_compact hook -------------------------------------------------------------------


async def test_a_cancelling_hook_drops_the_previous_summary_and_keeps_the_carried_lead() -> None:
    # CURRENT BEHAVIOUR: a cancelled compaction renders *without* the previous summary, although
    # the un-forced path a moment earlier would have rendered it.
    session, _ = await _with_old_checkpoint("acme:char-cancel")
    get_agent_hooks().register_before_compact(lambda prep, ctx: {"cancel": True})
    model = _Model()

    out = await _forced(session, model, keep=1)

    assert _shape(out) == [
        SYS,
        ("user", "q1", None, None),
        ("assistant", "a1", None, None),
        ("user", "q2", None, None),
        ("assistant", "a2", None, None),
        NEXT,
    ]
    assert not model.calls
    assert len(await _checkpoints(session)) == 1, "a cancelled compaction persisted a checkpoint"


async def test_a_hook_compaction_block_supplies_the_summary_and_its_usage() -> None:
    get_agent_hooks().register_before_compact(
        lambda prep, ctx: {"compaction": {"summary": "HOOKED", "usage": {"input": 3}}}
    )
    session, _ = await _session("acme:char-hook-usage", [_u("q0"), _a("a0"), _u("q1"), _a("a1")])
    model = _Model()

    out = await _forced(session, model, keep=1)

    assert _shape(out) == [
        SYS,
        _summary("HOOKED"),
        ("user", "q1", None, None),
        ("assistant", "a1", None, None),
        NEXT,
    ]
    assert not model.calls
    (checkpoint,) = await _checkpoints(session)
    assert checkpoint.content == "HOOKED"
    assert checkpoint.metadata["usage"] == {"input": 3}
    assert checkpoint.metadata["reason"] == "threshold"


# -- summariser failure paths ------------------------------------------------------------------


async def test_a_failed_history_summary_drops_the_previous_summary_and_persists_nothing() -> None:
    # CURRENT BEHAVIOUR: the failure frame passes no summary, so the previous one (`OLD`) is not
    # rendered -- unlike the no-model frame below, which keeps it. The carried lead (q1) is gone
    # too: the frame leads with the new cut's opening only.
    session, _ = await _with_old_checkpoint("acme:char-fail")
    failed: list[dict[str, Any]] = []
    get_agent_hooks().register_compact_failed(lambda info, ctx: failed.append(info))

    out = await _forced(session, _Model(fail=True), keep=1, will_retry=True, compact_reason="overflow")

    assert _shape(out) == [
        SYS,
        ("system", "[session] compaction failed; kept 1 recent events (dropped 2).", None, None),
        ("user", "q2", None, None),
        ("assistant", "a2", None, None),
        NEXT,
    ]
    assert failed == [
        {"reason": "overflow", "errorMessage": "provider down", "aborted": False, "willRetry": True}
    ]
    assert len(await _checkpoints(session)) == 1


async def test_no_model_keeps_the_previous_summary_and_notes_the_kept_tokens() -> None:
    session, _ = await _with_old_checkpoint("acme:char-no-model")
    failed: list[dict[str, Any]] = []
    get_agent_hooks().register_compact_failed(lambda info, ctx: failed.append(info))

    out = await _forced(session, None, keep=1)

    assert _shape(out) == [
        SYS,
        (
            "system",
            "[session] compaction unavailable (no model); kept ~1 recent tokens (dropped 2 older events).",
            None,
            None,
        ),
        _summary("OLD"),
        ("user", "q2", None, None),
        ("assistant", "a2", None, None),
        NEXT,
    ]
    assert failed == [
        {"reason": "threshold", "errorMessage": "no_model", "aborted": False, "willRetry": False}
    ]
    assert len(await _checkpoints(session)) == 1


# -- the split turn's prefix summary -----------------------------------------------------------

_PREFIX_FAILED = (
    "[session] summarising the earlier steps of this turn failed; kept its opening message, dropped 2 events."
)
_KEPT_STEPS = [_call_msg(1), _result_msg(1), _call_msg(2), _result_msg(2)]
_KEPT_ITEMS = [_call_item(1), _result_item(1), _call_item(2), _result_item(2)]


async def test_a_turn_prefix_with_no_model_keeps_the_opening_and_persists_an_empty_summary() -> None:
    session, ids = await _session("acme:char-prefix-no-model", [_u("REQ"), *_steps(0, 1, 2)])

    out = await _forced(session, None, keep=60)

    assert _shape(out) == [
        SYS,
        ("system", _PREFIX_FAILED, None, None),
        ("user", "REQ", None, None),
        *_KEPT_STEPS,
        NEXT,
    ]
    (checkpoint,) = await _checkpoints(session)
    assert checkpoint.content == ""
    assert checkpoint.metadata == {
        "type": "compaction",
        "covers_to_seq": 2,
        "first_kept_seq": 3,
        "first_kept_entry_id": ids[3],
        "last_kept_entry_id": ids[6],
        "tokens_before": 63,
        "retainedTail": [_item("user", "REQ"), *_KEPT_ITEMS],
        "details": {"readFiles": ["f0"], "modifiedFiles": []},
        "is_split_turn": True,
        "reason": "threshold",
        "split_turn": {"opening_kept": True, "prefix_summarized": False, "lead_items": 1},
    }


async def test_a_pinned_opening_with_no_model_has_no_lead_and_persists_nothing() -> None:
    # CURRENT BEHAVIOUR: nothing is persisted, so the next render compacts again from scratch.
    session, _ = await _session(
        "acme:char-pinned-no-model", [_u("REQ", metadata={"pinned": True}), *_steps(0, 1, 2)]
    )

    out = await _forced(session, None, keep=60)

    assert _shape(out) == [
        SYS,
        ("system", _PREFIX_FAILED, None, None),
        ("user", "REQ", None, None),
        *_KEPT_STEPS,
        NEXT,
    ]
    assert await _checkpoints(session) == []


async def test_a_pinned_opening_is_led_by_its_progress_and_lost_on_every_later_render() -> None:
    # CURRENT BEHAVIOUR, twice over: on the turn it is made, the progress summary is rendered
    # *ahead of* the pinned request it summarises (the lead precedes the events). And the pinned
    # request is in neither the checkpoint's tail nor the log past it, so every later render --
    # the replay and a second compaction alike -- has no request at all.
    session, _ = await _session(
        "acme:char-pinned-model", [_u("REQ", metadata={"pinned": True}), *_steps(0, 1, 2)]
    )
    model = _Model()

    made = await _forced(session, model, keep=60)

    assert _shape(made) == [SYS, _prefix("PFX"), ("user", "REQ", None, None), *_KEPT_STEPS, NEXT]
    assert [c[1][1] for c in model.calls] == [
        fence_untrusted(
            f"[User]: REQ\n[Assistant tool calls]: read_file({{'path': 'f0'}})\n[Tool result]: {_body(0)}"
        )
    ]
    (checkpoint,) = await _checkpoints(session)
    assert checkpoint.content == ""
    assert checkpoint.metadata["retainedTail"] == [
        _item("user", turn_prefix_message("PFX").content or ""),
        *_KEPT_ITEMS,
    ]
    assert checkpoint.metadata["split_turn"] == {
        "opening_kept": False,
        "prefix_summarized": True,
        "lead_items": 1,
    }
    assert checkpoint.metadata["turn_prefix_usage"] == {"metered": "compaction_turn_prefix"}
    assert "usage" not in checkpoint.metadata

    replayed = await _roomy(60).render(session, INCOMING, {"system_prompt": "sys", "model": model})
    assert _shape(replayed) == [SYS, _prefix("PFX"), *_KEPT_STEPS, NEXT]

    await annotate_and_append(session, _steps(3, 4))
    model = _Model(prefix="PFX2")
    again = await _forced(session, model, keep=60)

    assert _shape(again) == [
        SYS,
        _prefix("PFX2"),
        _call_msg(3),
        _result_msg(3),
        _call_msg(4),
        _result_msg(4),
        NEXT,
    ]
    # The stored lead (prefix only) is folded into the second prefix call, under an empty request.
    assert [c[1][1] for c in model.calls] == [
        fence_untrusted(
            "[User]: \n"
            f"[Earlier in this turn, summarised]: {turn_prefix_message('PFX').content}\n"
            "[Assistant tool calls]: read_file({'path': 'f1'})\n"
            f"[Tool result]: {_body(1)}\n"
            "[Assistant tool calls]: read_file({'path': 'f2'})\n"
            f"[Tool result]: {_body(2)}"
        )
    ]
    second = (await _checkpoints(session))[-1]
    assert second.content == ""
    assert second.metadata["covers_to_seq"] == 6
    assert second.metadata["first_kept_seq"] == 8
    assert second.metadata["split_turn"] == {
        "opening_kept": False,
        "prefix_summarized": True,
        "lead_items": 1,
    }
    assert second.metadata["details"] == {"readFiles": ["f1", "f2"], "modifiedFiles": []}


_SPLIT_WITH_HISTORY = [_u("old"), _a("ok"), _u("REQ"), *_steps(0, 1, 2)]


async def test_an_empty_turn_prefix_summary_is_a_failure_and_its_usage_is_not_stored() -> None:
    session, _ = await _session("acme:char-prefix-empty", list(_SPLIT_WITH_HISTORY))

    out = await _forced(session, _Model(prefix=""), keep=60)

    assert _shape(out) == [
        SYS,
        ("system", _PREFIX_FAILED, None, None),
        _summary("HIST"),
        ("user", "REQ", None, None),
        *_KEPT_STEPS,
        NEXT,
    ]
    (checkpoint,) = await _checkpoints(session)
    assert checkpoint.content == "HIST"
    assert checkpoint.metadata["split_turn"] == {
        "opening_kept": True,
        "prefix_summarized": False,
        "lead_items": 1,
    }
    assert checkpoint.metadata["usage"] == {"metered": "compaction"}
    assert "turn_prefix_usage" not in checkpoint.metadata


async def test_both_summaries_store_their_usage_under_their_own_keys() -> None:
    session, ids = await _session("acme:char-prefix-usage", list(_SPLIT_WITH_HISTORY))

    out = await _forced(session, _Model(), keep=60)

    assert _shape(out) == [
        SYS,
        _summary("HIST"),
        ("user", "REQ", None, None),
        _prefix("PFX"),
        *_KEPT_STEPS,
        NEXT,
    ]
    (checkpoint,) = await _checkpoints(session)
    assert checkpoint.metadata == {
        "type": "compaction",
        "covers_to_seq": 4,
        "first_kept_seq": 5,
        "first_kept_entry_id": ids[5],
        "last_kept_entry_id": ids[8],
        "tokens_before": 65,
        "retainedTail": [
            _item("user", "REQ"),
            _item("user", turn_prefix_message("PFX").content or ""),
            *_KEPT_ITEMS,
        ],
        "details": {"readFiles": ["f0"], "modifiedFiles": []},
        "is_split_turn": True,
        "reason": "threshold",
        "split_turn": {"opening_kept": True, "prefix_summarized": True, "lead_items": 2},
        "usage": {"metered": "compaction"},
        "turn_prefix_usage": {"metered": "compaction_turn_prefix"},
    }


async def test_a_metering_failure_still_stores_the_summary_without_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import felix.patterns.model as model_mod

    def _boom(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("meter down")

    monkeypatch.setattr(model_mod, "record_model_usage", _boom)
    session, _ = await _session("acme:char-meter", [_u("q0"), _a("a0"), _u("q1"), _a("a1")])

    out = await _forced(session, _Model(), keep=1)

    assert _shape(out) == [
        SYS,
        _summary("HIST"),
        ("user", "q1", None, None),
        ("assistant", "a1", None, None),
        NEXT,
    ]
    (checkpoint,) = await _checkpoints(session)
    assert checkpoint.content == "HIST"
    assert "usage" not in checkpoint.metadata


# -- reading a stored summary back -------------------------------------------------------------


async def test_a_malformed_split_lead_is_ignored_and_a_non_dict_tail_item_skipped() -> None:
    session, ids = await _session("acme:char-bad-lead", [_u("q0"), _a("a0"), _u("q1"), _a("a1")])
    await session.append(
        AppendableEvent(
            kind="compaction",
            content="OLD",
            metadata={
                "type": "compaction",
                "covers_to_seq": 1,
                "split_turn": {"opening_kept": True, "lead_items": 5},
                "retainedTail": [
                    {"role": "user", "content": "q1"},
                    {"role": "assistant", "content": "a1"},
                    "junk",
                ],
            },
        )
    )
    expected = [SYS, _summary("OLD"), ("user", "q1", None, None), ("assistant", "a1", None, None), NEXT]

    replayed = await _roomy().render(session, INCOMING, {"system_prompt": "sys", "model": _Model()})
    assert _shape(replayed) == expected

    model = _Model()
    forced = await _forced(session, model, keep=1)

    assert _shape(forced) == expected
    assert not model.calls, "nothing new before the cut turn: the previous summary stands"
    latest = (await _checkpoints(session))[-1]
    assert latest.content == "OLD"
    assert latest.metadata == {
        "type": "compaction",
        "covers_to_seq": 2,
        "first_kept_seq": 3,
        "first_kept_entry_id": ids[3],
        "last_kept_entry_id": ids[3],
        "tokens_before": 32,
        "retainedTail": [_item("user", "q1"), _item("assistant", "a1")],
        "details": NO_FILES,
        "is_split_turn": True,
        "reason": "threshold",
        "split_turn": {"opening_kept": True, "prefix_summarized": False, "lead_items": 1},
    }


async def test_a_summary_written_on_a_rewound_branch_is_not_rendered() -> None:
    session, ids = await _session(
        "acme:char-rewind", [_u("q0"), _a("a0"), _u("q1"), _a("a1"), _u("q2"), _a("a2")]
    )
    await _forced(session, _Model(history="STALE"), keep=1)
    await rewind_to(session, ids[1])
    await annotate_and_append(session, [_u("q1b"), _a("a1b")])

    out = await _roomy().render(session, INCOMING, {"system_prompt": "sys", "model": _Model()})

    assert _shape(out) == [
        SYS,
        ("user", "q0", None, None),
        ("assistant", "a0", None, None),
        ("user", "q1b", None, None),
        ("assistant", "a1b", None, None),
        NEXT,
    ]


_SIX = [_u("q0"), _a("a0"), _u("q1"), _a("a1"), _u("q2"), _a("a2")]


async def _legacy(thread: str, metadata: dict[str, Any]) -> list[ChatMessage]:
    """An audit-kind summary with no `retainedTail`, the shape written before checkpoints."""
    session, ids = await _session(thread, list(_SIX))
    md = {k: (ids[v] if k == "first_kept_entry_id" else v) for k, v in metadata.items()}
    await session.append(
        AppendableEvent(kind="audit", content="LEGACY", metadata={"type": "session_summary", **md})
    )
    return await _roomy().render(session, INCOMING, {"system_prompt": "sys", "model": _Model()})


async def test_a_legacy_summary_resumes_from_its_first_kept_entry() -> None:
    # `covers_to_seq: 0` reads as absent (CURRENT BEHAVIOUR, see below); the entry id decides.
    out = await _legacy("acme:char-legacy-id", {"covers_to_seq": 0, "first_kept_entry_id": 3})

    assert _shape(out) == [
        SYS,
        _summary("LEGACY"),
        ("assistant", "a1", None, None),
        ("user", "q2", None, None),
        ("assistant", "a2", None, None),
        NEXT,
    ]


async def test_a_legacy_summary_with_only_first_kept_seq_drops_that_event() -> None:
    # CURRENT BEHAVIOUR: with no `covers_to_seq`, `first_kept_seq` is taken as the last
    # *covered* seq, so the first kept event (q1, seq 2) is filtered out -- and the entry id
    # that names it cannot rescue it, because it is looked up among the events already filtered.
    out = await _legacy("acme:char-legacy-seq", {"first_kept_seq": 2, "first_kept_entry_id": 2})

    assert _shape(out) == [
        SYS,
        _summary("LEGACY"),
        ("assistant", "a1", None, None),
        ("user", "q2", None, None),
        ("assistant", "a2", None, None),
        NEXT,
    ]


async def test_a_summary_covering_seq_zero_renders_seq_zero_again() -> None:
    # CURRENT BEHAVIOUR: `int(covers_to_seq or first_kept_seq or -1)` treats a 0 as absent, so
    # the event the summary covers (q0) is rendered after it as well.
    out = await _legacy("acme:char-legacy-zero", {"covers_to_seq": 0})

    assert _shape(out) == [
        SYS,
        _summary("LEGACY"),
        *[(e.role, e.content, None, None) for e in _SIX],
        NEXT,
    ]


# -- what the summariser reads, and the file ops it records -------------------------------------


async def test_the_summariser_transcript_and_the_recorded_file_ops() -> None:
    session, _ = await _session(
        "acme:char-serialize",
        [
            _u('please read(path="notes.md")'),
            _a("writing now", tool_calls=[{"id": "w1", "name": "write_file", "args": {"path": "out.py"}}]),
            AppendableEvent(
                kind="tool_result", role="tool", content="B" * 2100, tool_call_id="w1", name="write_file"
            ),
            AppendableEvent(kind="custom", content="CUSTOM", metadata={"in_context": True}),
            AppendableEvent(
                kind="message", role="assistant", name="edit_file", content='done edit(file="x.py")'
            ),
            _u("latest"),
            _a("fine"),
        ],
    )
    model = _Model()

    out = await _forced(session, model, keep=2)

    assert _shape(out) == [
        SYS,
        _summary("HIST"),
        ("user", "latest", None, None),
        ("assistant", "fine", None, None),
        NEXT,
    ]
    (call,) = model.calls
    assert call[1] == (
        "user",
        fence_untrusted(
            '[User]: please read(path="notes.md")\n'
            "[Assistant]: writing now\n"
            "[Assistant tool calls]: write_file({'path': 'out.py'})\n"
            f"[Tool result]: {'B' * 2000}\n...[truncated 100 chars]\n"
            "[custom]: CUSTOM\n"
            '[Assistant]: done edit(file="x.py")'
        ),
    )
    (checkpoint,) = await _checkpoints(session)
    assert checkpoint.metadata["details"] == {"readFiles": ["notes.md"], "modifiedFiles": ["out.py", "x.py"]}
    assert checkpoint.metadata["is_split_turn"] is False
    assert "split_turn" not in checkpoint.metadata


async def test_a_hook_with_an_empty_summary_falls_through_to_the_model() -> None:
    get_agent_hooks().register_before_compact(lambda prep, ctx: {"summary": "", "usage": {"input": 9}})
    session, _ = await _session("acme:char-hook-empty", [_u("q0"), _a("a0"), _u("q1"), _a("a1")])
    model = _Model()

    out = await _forced(session, model, keep=1)

    assert _shape(out) == [
        SYS,
        _summary("HIST"),
        ("user", "q1", None, None),
        ("assistant", "a1", None, None),
        NEXT,
    ]
    assert len(model.calls) == 1
    (checkpoint,) = await _checkpoints(session)
    assert checkpoint.metadata["usage"] == {"metered": "compaction"}
