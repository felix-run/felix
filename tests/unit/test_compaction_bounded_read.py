"""`_load_branch` reads full rows only past the summary, and renders what a whole-log read renders.

The log's *shape* comes from `get_event_skeletons`: seq, kind, role and `SKELETON_METADATA_KEYS`.
A key that list lacks does not fail loudly -- the summary goes unfound and the render falls back
to reading the whole log, correct and as slow as before -- or, for the keys that rule a summary
*off* the branch, picks a stale one. So each scenario below asserts where the full read started,
and `_render_both` asserts the output matches a render with the skeletons truly hidden; each key in
the list is the one some scenario here cannot do without.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.patterns.model import ModelChatResult
from felix.patterns.types import ChatMessage
from felix.session.compaction import CompactingSessionStrategy
from felix.session.store import InMemorySessionStore
from felix.session.tree import annotate_and_append, rewind_to
from felix.session.types import AppendableEvent, GetEventsOpts

INCOMING = [ChatMessage(role="user", content="next")]


class _Model:
    def __init__(self, text: str) -> None:
        self.text = text

    async def chat(self, messages: list[ChatMessage], tools: Any, opts: Any = None) -> ModelChatResult:
        return ModelChatResult(
            message=ChatMessage(role="assistant", content=self.text), stop_reason="end_turn"
        )


@pytest.fixture(autouse=True)
def _unmetered(monkeypatch: pytest.MonkeyPatch) -> None:
    import felix.patterns.model as model_mod

    monkeypatch.setattr(model_mod, "record_model_usage", lambda *a, **kw: None)


class _Reads:
    """A session that records where each full read started; optionally hides its skeletons."""

    def __init__(self, inner: Any, *, skeletons: bool = True) -> None:
        self._inner = inner
        self._skeletons = skeletons
        self.id = inner.id
        self.full_reads: list[int | None] = []
        self.skeleton_reads = 0

    async def get_events(self, opts: GetEventsOpts | None = None) -> list[Any]:
        self.full_reads.append(opts.from_seq if opts else None)
        return await self._inner.get_events(opts)

    async def _shape(self) -> list[Any]:
        self.skeleton_reads += 1
        return await self._inner.get_event_skeletons()

    def __getattr__(self, name: str) -> Any:
        # Hidden for real: falling through to the inner session here once made the "whole-log"
        # arm take the skeleton path too, so comparing the two compared it with itself.
        if name == "get_event_skeletons":
            if not self._skeletons:
                raise AttributeError(name)
            return self._shape
        return getattr(self._inner, name)


def _u(text: str) -> AppendableEvent:
    return AppendableEvent(kind="message", role="user", content=text)


def _a(text: str) -> AppendableEvent:
    return AppendableEvent(kind="message", role="assistant", content=text)


def _strategy(keep: int = 400) -> CompactingSessionStrategy:
    return CompactingSessionStrategy(
        reserve_tokens=10, keep_recent_tokens=keep, context_window_tokens=10_000_000
    )


def _shape(out: list[ChatMessage]) -> list[tuple[Any, Any]]:
    return [(m.role, m.content) for m in out]


async def _render_both(session: Any) -> tuple[list[tuple[Any, Any]], _Reads]:
    """Render through skeletons and through the whole log; assert they agree."""
    opts = {"system_prompt": "sys", "model": _Model("unused")}
    bounded = _Reads(session)
    out = await _strategy().render(bounded, INCOMING, opts)
    hidden = _Reads(session, skeletons=False)
    whole = await _strategy().render(hidden, INCOMING, opts)
    assert hidden.full_reads == [None] and hidden.skeleton_reads == 0
    assert bounded.skeleton_reads == 1
    assert _shape(out) == _shape(whole)
    return _shape(out), bounded


async def _six(thread: str) -> tuple[Any, list[str]]:
    session = InMemorySessionStore(tenant_id="acme").open(thread)
    ids = await annotate_and_append(session, [_u("q0"), _a("a0"), _u("q1"), _a("a1"), _u("q2"), _a("a2")])
    return session, ids


async def test_after_a_compaction_only_the_summary_on_is_read_whole() -> None:
    """Needs `event_id`, `parent_id` and `covers_to_seq`: the walk, and where the summary ends."""
    session, _ = await _six("acme:bounded-checkpoint")
    await _strategy(keep=1).compact_now(session, model=_Model("OLD"))
    await annotate_and_append(session, [_u("q3"), _a("a3")])
    checkpoint = next(e for e in await session.get_events() if e.kind == "compaction")
    covered = int(checkpoint.metadata["covers_to_seq"])

    out, reads = await _render_both(session)

    assert reads.full_reads == [min(checkpoint.seq, covered + 1)]
    assert ("user", "q3") in out and ("user", "q0") not in out


async def test_a_legacy_audit_summary_is_found_by_its_type() -> None:
    """Needs `type`: an audit-kind summary is a summary only by its metadata type."""
    session, ids = await _six("acme:bounded-legacy")
    await session.append(
        AppendableEvent(
            kind="audit",
            content="LEGACY",
            metadata={"type": "session_summary", "covers_to_seq": 2, "first_kept_entry_id": ids[3]},
        )
    )
    _, reads = await _render_both(session)
    assert reads.full_reads == [3]


async def test_a_legacy_summary_with_only_first_kept_seq_bounds_the_read_by_it() -> None:
    """Needs `first_kept_seq`: with no `covers_to_seq`, it is what the summary covers."""
    session, _ = await _six("acme:bounded-first-kept-seq")
    await session.append(
        AppendableEvent(
            kind="audit", content="LEGACY", metadata={"type": "session_summary", "first_kept_seq": 2}
        )
    )
    _, reads = await _render_both(session)
    assert reads.full_reads == [3]


async def test_a_summary_whose_first_kept_entry_was_rewound_away_is_not_used() -> None:
    """Needs `first_kept_entry_id`: what it covers is still on the branch, what it kept is not."""
    session, ids = await _six("acme:bounded-first-kept-id")
    await session.append(
        AppendableEvent(
            kind="audit",
            content="STALE",
            metadata={"type": "session_summary", "covers_to_seq": 1, "first_kept_entry_id": ids[4]},
        )
    )
    await rewind_to(session, ids[3])
    await annotate_and_append(session, [_u("q2b"), _a("a2b")])

    out, _ = await _render_both(session)
    assert all("STALE" not in (content or "") for _, content in out)


async def test_a_summary_whose_last_kept_entry_was_rewound_away_is_not_used() -> None:
    """Needs `last_kept_entry_id`: the first kept entry survived the rewind, the last did not."""
    session, ids = await _six("acme:bounded-last-kept-id")
    await session.append(
        AppendableEvent(
            kind="audit",
            content="STALE",
            metadata={
                "type": "session_summary",
                "covers_to_seq": 1,
                "first_kept_entry_id": ids[2],
                "last_kept_entry_id": ids[5],
            },
        )
    )
    await rewind_to(session, ids[3])
    await annotate_and_append(session, [_u("q2b"), _a("a2b")])

    out, _ = await _render_both(session)
    assert all("STALE" not in (content or "") for _, content in out)


async def test_with_no_summary_the_shape_is_read_and_then_the_whole_log() -> None:
    """The price of the bounded read on a thread that never compacted: one extra, light query."""
    session, _ = await _six("acme:bounded-none")
    _, reads = await _render_both(session)
    assert reads.full_reads == [None]
    assert reads.skeleton_reads == 1


async def test_a_summary_covering_past_its_own_seq_is_still_read_whole() -> None:
    """`min(summary.seq, covered + 1)`: a legacy or forked summary can cover seqs after its own,
    and it must come back whole or it renders as nothing."""
    session, _ = await _six("acme:bounded-covers-past")
    await session.append(
        AppendableEvent(
            kind="audit", content="LEGACY", metadata={"type": "session_summary", "covers_to_seq": 7}
        )
    )
    await annotate_and_append(session, [_u("q3"), _a("a3"), _u("q4")])

    out, reads = await _render_both(session)

    assert reads.full_reads == [6]
    assert any("LEGACY" in (content or "") for _, content in out)


async def test_an_older_checkpoint_still_on_the_branch_bounds_the_read_after_a_rewind() -> None:
    """Two checkpoints; a rewind abandons the newer. The older one, and its tail, still render."""
    session, ids = await _six("acme:bounded-two-checkpoints")
    await _strategy(keep=1).compact_now(session, model=_Model("A"))
    ids += await annotate_and_append(session, [_u("q3"), _a("a3")])
    await _strategy(keep=1).compact_now(session, model=_Model("B"))
    first, _newer = [e for e in await session.get_events() if e.kind == "compaction"]
    await rewind_to(session, ids[6])
    await annotate_and_append(session, [_u("q3b"), _a("a3b")])

    out, reads = await _render_both(session)

    covered = int(first.metadata["covers_to_seq"])
    assert reads.full_reads == [min(first.seq, covered + 1)]
    summaries = [content for _, content in out if "<conversation_summary>" in (content or "")]
    assert summaries == [summaries[0]] and "\nA\n" in summaries[0]
    # Rewound to q3: the turn after it, and the checkpoint written past it, are gone.
    assert ("user", "q3b") in out and ("assistant", "a3") not in out


async def test_rows_deleted_between_the_two_reads_never_render_as_empty_messages() -> None:
    """A thread cleared mid-render: the shape was read, the full rows are gone."""
    session, _ = await _six("acme:bounded-race")
    await _strategy(keep=1).compact_now(session, model=_Model("OLD"))
    await annotate_and_append(session, [_u("q3"), _a("a3")])

    class _ClearedMidRender(_Reads):
        async def get_events(self, opts: GetEventsOpts | None = None) -> list[Any]:
            return []

    out = await _strategy().render(
        _ClearedMidRender(session), INCOMING, {"system_prompt": "sys", "model": _Model("unused")}
    )
    # Nothing survives of the deleted thread but the frame: no empty messages, no summary.
    assert _shape(out) == [("system", "sys"), ("user", "next")]
