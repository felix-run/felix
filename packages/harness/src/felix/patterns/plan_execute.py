"""The `plan_execute` pattern's own vocabulary: what a plan is, and when one has failed.

Split out of `patterns/delegating.py` the way `plan_tools.py` was split out of the package
entry point — `delegating.py` holds one `_run_*` per composite pattern and the plumbing they
share, so a pattern's own definitions crowd it. The step loop stays there, because it is a
`_DelegatingAgent` method like the other five; everything a reader needs in order to answer
"what counts as a failed subtask" or "what is a replan asked for" is here.

Nothing here imports `delegating`, so the dependency runs one way.
"""

from __future__ import annotations

from felix_ai.types import ChatMessage, ModelClient

from felix.patterns.model import record_model_usage

#: Stop reasons that mean a subtask did not finish the work it was given.
#:
#: Deliberately narrow: `refusal` is governance replacing the reply and `max_tokens` is the
#: model cut off mid-answer, and in both the note the synthesiser would record is not an
#: answer. An empty reply is *not* here -- that is a step that ran to completion and produced
#: little, a planning problem rather than a failure, and replanning on it would loop on
#: subtasks that are simply hard to say anything about.
#:
#: One real failure this does **not** catch, named rather than left to be discovered: an
#: executor that exhausts `executor_recursion_limit` ends on `tool_use`, because react's last
#: assistant message carried tool calls. `tool_use` cannot simply be added -- a run that ends
#: through a terminal tool (`all_terminate`) reports it too, and that is a success. Telling
#: them apart needs `final.tool_calls` being non-empty, or a stop reason react does not
#: currently raise. Until then a subtask that runs out of steps records its tool preamble as
#: the answer and does not replan.
_FAILED_STOP_REASONS = frozenset({"refusal", "max_tokens"})


async def _plan_subtasks(
    planner: ModelClient,
    messages: list[ChatMessage],
    *,
    system_prompt: str,
    manifest_id: str,
    max_subtasks: int,
) -> list[str]:
    """One planning turn: ask for a numbered list, return it as bare subtasks.

    Both planning calls a run makes go through here — the opening plan and every replan —
    so the instruction, the numbering strip, and the `max_subtasks` truncation cannot come
    to differ between the plan a run starts with and the plan it recovers to.
    """
    result = await planner.chat(
        [
            ChatMessage(
                role="system",
                content=system_prompt or "Break the user goal into a numbered list of subtasks.",
            ),
            *messages,
            ChatMessage(
                role="user", content=f"Return at most {max_subtasks} numbered subtasks, one per line."
            ),
        ],
        [],
    )
    record_model_usage(result, planner, manifest_id=manifest_id)
    return [
        ln.strip().lstrip("0123456789.-) ").strip()
        for ln in result.message.content.splitlines()
        if ln.strip()
    ][:max_subtasks]


def _replan_request(
    messages: list[ChatMessage], notes: list[str], step: str, step_stop: str, index: int
) -> list[ChatMessage]:
    """The conversation a replan is asked from.

    The remainder, not the whole goal: the steps already done are in `notes` and redoing
    them would spend the budget twice. The failed step is *described* rather than repeated
    verbatim, so the planner can route around it instead of reissuing an instruction that
    has already not worked once.
    """
    return [
        *messages,
        ChatMessage(
            role="user",
            content=("Progress so far:\n" + ("\n".join(notes) or "(nothing yet)"))
            + f"\n\nSubtask {index + 1} ({step}) did not complete: {step_stop}."
            + "\nReturn a revised numbered list for the remaining work only.",
        ),
    ]


def _step_failed(stop_reason: str) -> bool:
    return stop_reason in _FAILED_STOP_REASONS
