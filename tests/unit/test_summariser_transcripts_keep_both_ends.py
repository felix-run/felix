"""Every summariser is shown both ends of a transcript too long to show whole.

The cap (`compaction.SUMMARIZER_INPUT_CHARS`) used to keep a transcript's head, so a summary
for continued work was written from how the conversation began and never saw where it had got
to. `compaction.clip_transcript` keeps the first quarter and the last three quarters and marks
the cut. These pin it at each summariser that reads a transcript: compaction's history and
turn-prefix passes and the branch summary. (`summarizing:N` is pinned beside its cache test in
`test_composite_handoffs_are_fenced.py`.)
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.patterns.model import ModelChatResult, TokenUsage
from felix.patterns.types import ChatMessage
from felix.session.compaction import SUMMARIZER_INPUT_CHARS, clip_transcript
from felix.session.types import SessionEvent

LONG = SUMMARIZER_INPUT_CHARS  # each event alone is as long as the cap


class _Summarizer:
    model_id = "summarizer"

    def __init__(self) -> None:
        self.asked: list[list[ChatMessage]] = []

    async def chat(self, messages: list[ChatMessage], tools: list[Any], opts: Any = None) -> ModelChatResult:
        self.asked.append(list(messages))
        return ModelChatResult(
            message=ChatMessage(role="assistant", content="summary"),
            stop_reason="end_turn",
            usage=TokenUsage(),
        )

    def transcript(self) -> str:
        (call,) = self.asked
        return str(call[-1].content)


def _events(*names: str) -> list[SessionEvent]:
    """One long message per name, each starting `<name>-start` and ending `<name>-end`."""
    return [
        SessionEvent(
            seq=i, ts=float(i), kind="message", role="user", content=f"{n}-start {'x' * LONG} {n}-end"
        )
        for i, n in enumerate(names, start=1)
    ]


def _both_ends(shown: str, first: str, last: str) -> None:
    assert f"{first}-start" in shown, "the head was dropped"
    assert f"{last}-end" in shown, "the tail was dropped"
    assert "characters omitted" in shown


@pytest.mark.parametrize("size", [0, 10, 100])
def test_a_transcript_within_the_cap_is_shown_whole(size: int) -> None:
    assert clip_transcript("y" * size, limit=100) == "y" * size


def test_a_transcript_past_the_cap_keeps_its_first_quarter_and_last_three_quarters() -> None:
    text = "".join(f"{i:04d}" for i in range(1000))  # 4,000 characters, each position distinct
    head, marker, tail = clip_transcript(text, limit=400).split("\n\n")
    assert (head, tail) == (text[:100], text[-300:])
    assert marker == "[… 3600 characters omitted …]"


async def test_compactions_history_summary_sees_both_ends() -> None:
    from felix.session.compaction import _SplitPlan, _summarize_history

    model = _Summarizer()
    await _summarize_history(
        model, _SplitPlan(history=_events("a", "b")), previous=None, instructions=None, reason="t"
    )
    _both_ends(model.transcript(), "a", "b")


async def test_compactions_turn_prefix_summary_sees_both_ends() -> None:
    from felix.session.compaction import _SplitPlan, _summarize_turn_prefix

    model = _Summarizer()
    plan = _SplitPlan(history=[], opening_text="first-start the request", prefix=_events("a", "b"))
    await _summarize_turn_prefix(model, plan, instructions=None, reason="t")
    _both_ends(model.transcript(), "first", "b")


async def test_the_branch_summary_sees_both_ends() -> None:
    from felix.session.branch import summarize_abandoned_branch
    from felix.session.store import InMemorySessionStore
    from felix.session.tree import annotate_and_append
    from felix.session.types import AppendableEvent

    session = InMemorySessionStore(tenant_id="acme").open("acme:branch-ends")
    ids = await annotate_and_append(
        session,
        [
            AppendableEvent(kind="message", role="user", content="kept"),
            *(
                AppendableEvent(kind="message", role="assistant", content=f"{n}-start {'x' * LONG} {n}-end")
                for n in ("a", "b")
            ),
        ],
    )
    model = _Summarizer()

    await summarize_abandoned_branch(session, old_leaf_id=ids[-1], new_leaf_id=ids[0], model=model)

    _both_ends(model.transcript(), "a", "b")
