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


def _tool_messages(*contents: str) -> list[ChatMessage]:
    return [
        ChatMessage(role="tool", tool_call_id=str(i), name="t", content=c) for i, c in enumerate(contents)
    ]


def _producer_spellings() -> set[str]:
    """Every literal a governance wrapper or the runner writes as a denial or error.

    Read off the source, so a new wrapper spelling its refusal a new way fails here rather
    than going uncounted by `max_errors`. Each hit is the text up to and including the first
    space after the opening bracket, or the closing bracket — enough to test the prefix.
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "packages/harness/src/felix"
    files = [root / "manifests/builder.py", root / "patterns/tool_runner.py", root / "tools/errors.py"]
    files += sorted((root / "governance").glob("*.py"))
    found: set[str] = set()
    for f in files:
        for m in re.finditer(
            r'(?:deny_output|tool_error_output|content=)\(?\s*f?"(\[[^"\]]*\]?)', f.read_text()
        ):
            found.add(m.group(1))
    assert found, "no denial literals found — has the producer spelling moved?"
    return found


def test_every_producer_spelling_is_recognised_as_a_failure() -> None:
    """`trajectory_of` only has the text. The vocabulary in `tools/types.py` must cover it."""
    from felix.tools.types import is_failure_content

    unrecognised = sorted(
        lit
        for lit in _producer_spellings()
        # `[cancelled] …` is the steer interrupting a batch, not a failure, and `[quarantined]`
        # is screening degrading a result rather than refusing it.
        if not lit.startswith(("[cancelled]", "[quarantined]")) and not is_failure_content(lit + " x")
    )
    assert unrecognised == [], f"denial spellings max_errors would not count: {unrecognised}"


@pytest.mark.parametrize(
    "content",
    [
        "[policy denied] missing scopes",
        "[command denied] rm -rf",
        "[command approval needed] sudo",
        "[screening blocked] untrusted content",
        "[screening unavailable] screener down",
        "[limits] max tool calls reached",
        "[guardrails] PII blocked",
        "[judge denied] scored 0.2",
        "[approval required] pending",
        "[approval rejected by reviewer] no",
        "[error/timeout] slow",
        "[fatal/internal] boom",
        "[tool error/permission_denied] shell",
    ],
)
def test_each_denial_shape_counts_as_an_error(content: str) -> None:
    assert trajectory_of(_tool_messages(content)).errors == 1


def test_ordinary_and_non_failure_tool_output_does_not_count() -> None:
    assert (
        trajectory_of(
            _tool_messages('{"hits": []}', "[cancelled] steered", "[quarantined] text", "ok")
        ).errors
        == 0
    )


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
        ({"max_tool_calls": "2"}, Trajectory(("a", "b")), True, "nonempty"),
        ({"max_tool_calls": ""}, Trajectory(("a", "b", "c")), True, "nonempty"),
        ({"max_errors": -1}, Trajectory(), False, "invalid_rubric"),
        ({"tools_not_called": "write_file"}, Trajectory(), False, "invalid_rubric"),
        # Two trajectory rules that both reject: the first in precedence names the failure.
        ({"tools_called": ["z"], "max_tool_calls": 0}, Trajectory(("a",)), False, "tools_called"),
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


@pytest.mark.asyncio
async def test_a_run_whose_manifest_cannot_resolve_counts_every_item_as_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The column's own definition: failures that never reached the scorer. This path
    reached none of them, and reported error_count 0 until it was pinned."""
    from felix.eval import runner as runner_mod

    async def _boom(*a: object, **k: object) -> None:
        raise RuntimeError("no such manifest")

    monkeypatch.setattr(runner_mod, "resolve_tenant_manifest", _boom)
    settings = _settings()
    await eval_store.put_dataset(
        settings,
        "default",
        "two",
        description="",
        items=[{"user_input": "a", "rubric": {}}, {"user_input": "b", "rubric": {}}],
    )
    run = await start_run(
        settings, tools=None, tenant_id="default", dataset_name="two", candidate_manifest="missing"
    )
    assert (run["pass_count"], run["fail_count"], run["error_count"]) == (0, 2, 2)


def test_every_bundled_fixture_validates_and_names_tools_its_manifest_binds() -> None:
    """`contributor.json` is not run by CI, so a misspelt tool name would be found on the
    first paid run. Every fixture must validate, and every tool a trajectory rule names must
    be one the candidate manifest actually binds."""
    import json
    from pathlib import Path

    from felix.eval.validation import validate_items
    from felix.manifests.loader import load_bundled

    fixtures = Path(__file__).resolve().parents[2] / "fixtures" / "eval"
    seen = 0
    for path in sorted(fixtures.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        report = validate_items(payload["items"])
        assert report.errors == [], f"{path.name}: {report.errors}"
        seen += 1
        if path.stem == "contributor":
            manifest = load_bundled("contributor")
            bound = set(manifest.spec.tools) | {ref.name for ref in manifest.spec.sandboxes}
            bound |= {ref.name for ref in getattr(manifest.spec, "shell_tools", [])}
            for item in payload["items"]:
                rubric = item["rubric"]
                named = set(rubric.get("tools_called") or []) | set(rubric.get("tools_not_called") or [])
                unbound = sorted(n for n in named if not n.startswith("github__") and n not in bound)
                assert unbound == [], f"{item['item_id']} names tools contributor does not bind: {unbound}"
    assert seen >= 3
