"""Trajectory rules score what a run did; `error_count` says what never got scored.

`_score_answer` is exercised directly with a `Trajectory`, and `trajectory_of` against the
message shapes the loop actually produces. The mock path is covered by the bundled fixtures
through `test_eval_gate_can_fail.py`; `error_count` is pinned end to end through `start_run`
on the in-memory store, which is the write path the API and CLI read.
"""

from __future__ import annotations

import pytest
from felix.config import Settings
from felix.eval import store as eval_store
from felix.eval.runner import Trajectory, _score_answer, start_run, trajectory_of
from felix.patterns.types import ChatMessage, ToolCall


def _settings() -> Settings:
    return Settings(database_url="memory://x", object_store="memory", auth_mode="none", allow_insecure=True)


@pytest.fixture(autouse=True)
def _clean() -> None:
    eval_store.reset_eval_for_tests() if hasattr(eval_store, "reset_eval_for_tests") else None


def test_trajectory_is_read_off_the_messages_the_loop_produces() -> None:
    msgs = [
        ChatMessage(role="user", content="hi"),
        ChatMessage(
            role="assistant", content="", tool_calls=[ToolCall(id="1", name="search_files", args={})]
        ),
        ChatMessage(role="tool", tool_call_id="1", name="search_files", content='{"hits": []}'),
        ChatMessage(role="assistant", content="", tool_calls=[ToolCall(id="2", name="write_file", args={})]),
        ChatMessage(
            role="tool", tool_call_id="2", name="write_file", content="[policy denied] missing scopes"
        ),
        ChatMessage(role="assistant", content="", tool_calls=[ToolCall(id="3", name="run", args={})]),
        ChatMessage(role="tool", tool_call_id="3", name="run", content="[error/timeout] took too long"),
        ChatMessage(role="assistant", content="done"),
    ]
    assert trajectory_of(msgs) == Trajectory(tool_names=("search_files", "write_file", "run"), errors=2)
    assert trajectory_of([ChatMessage(role="assistant", content="no tools")]) == Trajectory()


@pytest.mark.parametrize(
    ("rubric", "traj", "ok", "rule"),
    [
        ({"tools_called": ["a"]}, Trajectory(("a", "b")), True, "nonempty"),
        ({"tools_called": ["a", "c"]}, Trajectory(("a", "b")), False, "tools_called"),
        ({"tools_not_called": ["write_file"]}, Trajectory(("read_file",)), True, "nonempty"),
        ({"tools_not_called": ["write_file"]}, Trajectory(("write_file",)), False, "tools_not_called"),
        ({"max_tool_calls": 2}, Trajectory(("a", "b")), True, "nonempty"),
        ({"max_tool_calls": 1}, Trajectory(("a", "b")), False, "max_tool_calls"),
        ({"max_tool_calls": 0}, Trajectory(), True, "nonempty"),
        ({"max_errors": 0}, Trajectory(("a",), errors=1), False, "max_errors"),
        ({"max_errors": 1}, Trajectory(("a",), errors=1), True, "nonempty"),
        # A trajectory rule that holds still needs the answer rule to hold.
        ({"tools_called": ["a"], "contains": "yes"}, Trajectory(("a",)), False, "contains"),
        ({"tools_called": ["a"], "contains": "answer"}, Trajectory(("a",)), True, "contains"),
        # Shapes that could never reject fail closed.
        ({"tools_called": []}, Trajectory(("a",)), False, "invalid_rubric"),
        ({"tools_not_called": []}, Trajectory(), False, "invalid_rubric"),
        ({"max_tool_calls": -1}, Trajectory(), False, "invalid_rubric"),
        ({"max_errors": "many"}, Trajectory(), False, "invalid_rubric"),
        ({"tools_called": "search_files"}, Trajectory(("search_files",)), False, "invalid_rubric"),
    ],
)
def test_trajectory_rules(rubric: dict, traj: Trajectory, ok: bool, rule: str) -> None:
    got_ok, score, got_rule = _score_answer("the answer", rubric, traj)
    assert (got_ok, got_rule) == (ok, rule)
    assert score == (1.0 if ok else 0.0)


def test_no_trajectory_means_no_tools_ran() -> None:
    assert _score_answer("x", {"tools_called": ["a"]}) == (False, 0.0, "tools_called")
    assert _score_answer("x", {"tools_not_called": ["a"]}) == (True, 1.0, "nonempty")


@pytest.mark.asyncio
async def test_error_count_is_the_subset_of_failures_that_never_reached_the_scorer() -> None:
    settings = _settings()
    await eval_store.put_dataset(
        settings,
        "default",
        "mixed",
        description="",
        items=[
            {"item_id": "rejected", "user_input": "say ok", "rubric": {"expect": "ok", "mock_answer": "no"}},
            {"item_id": "raised", "user_input": "say ok", "rubric": "not a mapping"},
            {"item_id": "passed", "user_input": "say ok", "rubric": {"expect": "ok", "mock_answer": "ok"}},
        ],
    )
    run = await start_run(
        settings, tools=None, tenant_id="default", dataset_name="mixed", candidate_manifest="quick", mock=True
    )
    assert (run["pass_count"], run["fail_count"], run["error_count"]) == (1, 2, 1)
    stored = await eval_store.get_run(settings, "default", run["id"])
    assert stored is not None and stored["error_count"] == 1, "the row a reader sees carries it too"
    rows = {r["item_id"]: r for r in run["scores"]}
    assert "error" in rows["raised"] and "error" not in rows["rejected"]
    assert rows["passed"]["tool_calls"] == 0 and rows["passed"]["tool_errors"] == 0
