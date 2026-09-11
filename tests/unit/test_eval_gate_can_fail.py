"""The eval gate has to be able to say no.

CI runs `felix eval --dataset smoke --fixture fixtures/eval/smoke.json --mock` and treats a
non-zero exit as a failure. Every item in that fixture carries a `mock_answer` that satisfies
its own rubric, and `_mock_answer` returns it verbatim — so the run passes by construction. A
scorer rewritten to `return True, 1.0, "x"` would leave that step green, and so would one that
never ran at all.

This is the counter-smoke: a fixture whose every item must fail, and the assertion that it does.
Together the two say the gate can distinguish a right answer from a wrong one, which neither
says alone.

Three things have to hold for the gate to mean anything, and each has its own test below:
the scorer rejects a wrong answer; it rejects it *by scoring it*, not by raising, since
`start_run` counts a raised item as a failure too; and the CLI turns a failed run into a
non-zero exit, which is the only part CI actually reads.

It all runs here as well as in CI, because a gate that only exists in a workflow file is one
nobody can check before pushing.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
from typing import Any

import pytest
from felix.config import Settings
from felix.eval import store as eval_store
from felix.eval.runner import start_run

FIXTURES = pathlib.Path(__file__).resolve().parents[2] / "fixtures" / "eval"


@pytest.fixture(autouse=True)
def _isolate_process_settings() -> Any:
    """The CLI stamps a process role onto the cached `Settings` and loads optional plugins.

    Both are process-global and this is the only file here that invokes the CLI, so the cache is
    cleared on the way out rather than left for whatever runs next.
    """
    from felix.config import get_settings

    yield
    get_settings.cache_clear()


def _settings() -> Settings:
    return Settings(database_url="memory://eval-gate", object_store="memory")


def _scorer_rule_names() -> set[str]:
    """Every rule `_score_answer` can return, read off its own source.

    A hardcoded list here would let a new scoring rule land with the counter-smoke silently
    partial: the fixture would still cover the four old rules, every assertion would pass, and
    nothing would say the new rule had never been rejected by anything. Deriving the set means
    adding a rule to the scorer fails this file until a negative item exercises it.
    """
    import ast
    import inspect
    import textwrap

    from felix.eval import runner as runner_module

    tree = ast.parse(textwrap.dedent(inspect.getsource(runner_module._score_answer)))
    returns = [node for node in ast.walk(tree) if isinstance(node, ast.Return)]
    assert returns, "no returns found in _score_answer; has it moved or been renamed?"

    names: set[str] = set()
    unreadable: list[str] = []
    for node in returns:
        value = node.value
        if (
            isinstance(value, ast.Tuple)
            and value.elts
            and isinstance(value.elts[-1], ast.Constant)
            and isinstance(value.elts[-1].value, str)
        ):
            names.add(value.elts[-1].value)
        else:
            unreadable.append(ast.unparse(value) if value is not None else "return")

    # Every return, not merely the ones this recognises. A scanner that *filters* is how a rule
    # goes missing: `return _score_regex(answer, rubric)` or a rule name held in a variable
    # would each be dropped silently, the derived set would not grow, and the assertions built
    # on it would pass with the new rule never once seen rejecting anything — which is the
    # state deriving the set exists to make impossible.
    assert not unreadable, (
        f"_score_answer has returns this scanner cannot read a rule name from: {unreadable}. "
        "Return the rule as a literal in the tuple, or teach this helper the new shape — "
        "leaving it unread would quietly drop the rule from the counter-smoke's coverage."
    )
    # Ratcheted like the coverage floor, not a count that already trails reality: a rule
    # disappearing from the scorer should fail here rather than one rule below here.
    assert {"equals", "contains", "min_chars", "nonempty", "invalid_rubric"} <= names, names
    return names


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


async def _run_fixture(name: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Load a bundled fixture and score it exactly as `felix eval --mock` does.

    Returns the completed run alongside the fixture's items, so counts are asserted against
    the fixture's own length rather than a literal that a new item would falsify.
    """
    settings = _settings()
    payload = _fixture(name)

    await eval_store.put_dataset(
        settings,
        "default",
        payload["name"],
        description=payload.get("description", ""),
        items=payload["items"],
    )
    result = await start_run(
        settings,
        tools=None,
        tenant_id="default",
        dataset_name=payload["name"],
        candidate_manifest="quick",
        mock=True,
        deterministic_judge=True,
    )
    return result, list(payload["items"])


@pytest.mark.asyncio
async def test_the_smoke_fixture_passes_every_item() -> None:
    """One half of the pair, and the half CI already had."""
    result, items = await _run_fixture("smoke")

    assert result["fail_count"] == 0, result
    assert result["pass_count"] == len(items), result


@pytest.mark.asyncio
async def test_the_negative_fixture_fails_every_item() -> None:
    """The other half: a scorer that always passes turns this red and the smoke run green."""
    result, items = await _run_fixture("negative")

    assert result["pass_count"] == 0, result
    assert result["fail_count"] == len(items), result


@pytest.mark.asyncio
async def test_the_negative_items_are_scored_down_rather_than_erroring() -> None:
    """`fail_count` alone cannot tell a rejection from a crash.

    `start_run` catches a per-item exception, writes `{"pass": False, "error": ...}` and counts
    it as a failure — so three items that all raised satisfy the count assertion above exactly
    as well as three the scorer honestly rejected. Then the gate would be measuring that the
    mock path is broken, not that the scorer discriminates.

    The rule set is asserted here too: a fixture that failed five times through one rule would
    prove nothing about the other three, and this is the run rather than a direct call to the
    private scorer.
    """
    result, items = await _run_fixture("negative")

    rows = result["scores"]
    assert [row["pass"] for row in rows] == [False] * len(items), rows
    assert [row for row in rows if row.get("error")] == [], rows
    assert {row["rule"] for row in rows} == _scorer_rule_names(), rows


def test_a_failed_run_exits_non_zero_through_the_cli() -> None:
    """The exit code is the only part of the gate CI reads.

    Everything above asserts on `start_run`'s return value. CI asserts on `$?`. Deleting the
    three lines in `felix_cli/main.py` that turn `fail_count` into `SystemExit(1)` leaves every
    other test in this file green while the CI step it exists to back stops failing — the local
    half would be a strict subset of the CI half, which is the opposite of the point.
    """
    from felix_cli.main import app
    from typer.testing import CliRunner

    result = CliRunner().invoke(
        app,
        [
            "eval",
            "--dataset",
            "negative",
            "--manifest",
            "quick",
            "--fixture",
            str(FIXTURES / "negative.json"),
            "--mock",
        ],
    )

    # CliRunner reports exit_code 1 for *any* uncaught exception, so a FileNotFoundError from a
    # renamed fixture looks identical to the gate working. The exception type is the assertion.
    assert isinstance(result.exception, SystemExit), result.exception
    assert result.exception.code == 1, result.exception
    assert result.exit_code == 1, result.output
    # `scripts/eval-counter-smoke.sh` parses this same printed dict, so every string it greps
    # for is pinned here — otherwise the local half is a strict subset of the shell half and
    # the gate breaks in CI first, which is the arrangement this file exists to invert.
    assert "'fail_count'" in result.output, result.output
    assert "'pass_count': 0" in result.output, result.output
    assert "'rule'" in result.output, result.output
    assert "'error'" not in result.output, result.output


def test_the_smoke_fixture_exits_zero_through_the_cli() -> None:
    """The counter-assertion: an exit code stuck at 1 would pass the test above and fail CI."""
    from felix_cli.main import app
    from typer.testing import CliRunner

    result = CliRunner().invoke(
        app,
        [
            "eval",
            "--dataset",
            "smoke",
            "--manifest",
            "quick",
            "--fixture",
            str(FIXTURES / "smoke.json"),
            "--mock",
        ],
    )

    assert result.exit_code == 0, result.output


def test_the_shared_counter_smoke_script_rejects_a_run_that_did_not_reject(
    tmp_path: pathlib.Path,
) -> None:
    """The gate's own gate, run rather than read.

    `scripts/eval-counter-smoke.sh` is the one home of the four checks the CI eval job and
    `make check-ci` both make. An invariant asserts all four are present in it, which catches a
    check being deleted but not one hollowed to `|| true`, so the two cases they exist to
    refuse are executed here.

    Each case asserts the *message*, not just the exit status: every check in that script exits
    1, and so does the script when the eval never ran at all. A script hollowed the other way —
    refusing everything, including the real negative fixture — would pass a status-only
    assertion twice while turning CI permanently red.
    """
    root = FIXTURES.parents[1]
    script = root / "scripts" / "eval-counter-smoke.sh"

    # One item the scorer rejects honestly, one whose rubric is not a mapping so it errors.
    # That mix is the case the counts cannot see: the run still exits 1 with a pass count of 0
    # and prints score rows, and only the fourth check tells it from an honest rejection.
    erroring = tmp_path / "erroring.json"
    erroring.write_text(
        json.dumps(
            {
                "name": "negative",
                "items": [
                    {
                        "item_id": "rejected",
                        "user_input": "hi",
                        "rubric": {"expect": "ok", "mock_answer": "no"},
                    },
                    {"item_id": "boom", "user_input": "hi", "rubric": "not-a-mapping"},
                ],
            }
        ),
        encoding="utf-8",
    )

    cases = [
        (FIXTURES / "smoke.json", "expected exit 1 from the negative fixture, got 0"),
        (erroring, "errored instead of being scored down"),
    ]
    for fixture, expected in cases:
        done = subprocess.run(
            [str(script), str(fixture)],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        assert done.returncode == 1, f"the counter-smoke accepted {fixture.name}:\n{done.stdout}"
        assert expected in done.stderr, (
            f"{fixture.name} was refused, but not by the check this case exists for.\n"
            f"wanted: {expected}\ngot: {done.stderr}"
        )


def test_each_scoring_rule_is_exercised_in_both_directions() -> None:
    """Per rule, not just in aggregate.

    Passes and failures in aggregate could both come from one rule while the others were
    never reached — which is what a `contains`-only scorer would look like.
    """
    from felix.eval.runner import _score_answer

    cases = [
        ({"expect": "ok"}, "ok", "not ok", "equals"),
        # `equals` is an alias for `expect`; nothing else in the suite reaches it, so dropping
        # it from the scorer would otherwise be a silent no-op.
        ({"equals": "ok"}, "ok", "not ok", "equals"),
        # An empty expected answer is a real rubric, not an absent one. `_mock_answer` already
        # treats it that way, so a scorer that fell through here would disagree with the answer
        # generator it is scoring.
        ({"expect": ""}, "", "something", "equals"),
        ({"contains": "hello"}, "well hello there", "goodbye", "contains"),
        # Precedence, boundary by boundary: every other row names one key, so reordering the
        # branches in `_score_answer` would go unnoticed — and `_mock_answer` has a matching
        # order it would then contradict. One row per adjacent pair, so no swap is invisible.
        ({"expect": "ok", "contains": "zzz"}, "ok", "not ok", "equals"),
        ({"contains": "hello", "min_chars": 99}, "hello", "goodbye", "contains"),
        ({"min_chars": 4}, "long enough", "", "min_chars"),
        ({"min_chars": 32}, "x" * 32, "tiny", "min_chars"),
        ({}, "anything", "", "nonempty"),
    ]
    for rubric, good, bad, rule in cases:
        ok_good, score_good, name_good = _score_answer(good, rubric)
        ok_bad, score_bad, name_bad = _score_answer(bad, rubric)

        assert (ok_good, score_good, name_good) == (True, 1.0, rule), (rubric, good)
        assert (ok_bad, score_bad, name_bad) == (False, 0.0, rule), (rubric, bad)


def test_a_rubric_that_could_never_say_no_fails_closed() -> None:
    """An empty `contains` matches every answer, so it is a scorer that cannot reject.

    It is one unfilled field away in any hand-authored dataset, and it fails in the direction
    that hides problems: every item passes and the run looks healthy. Scoring it as a bad
    rubric keeps the eval honest about what it did not check.
    """
    from felix.eval.runner import _score_answer

    malformed = (
        {"contains": ""},
        {"contains": "   "},
        {"min_chars": -1},
        # Not a number at all. It used to raise, which made the item an *error* — and an errored
        # item is indistinguishable from a rejected one in the counts, so a malformed dataset
        # read as a working gate.
        {"min_chars": "abc"},
        # JSON parses `1e999` to infinity, which `int()` refuses with OverflowError rather than
        # the ValueError the first version of the guard caught.
        {"min_chars": float("inf")},
    )
    for rubric in malformed:
        for answer in ("anything at all", ""):
            assert _score_answer(answer, rubric) == (False, 0.0, "invalid_rubric"), (rubric, answer)


@pytest.mark.parametrize(
    "rubric",
    [
        {"expect": "ok"},
        {"equals": "ok"},
        {"expect": ""},
        {"contains": "hello"},
        {"min_chars": 32},
        {"min_chars": 0},
        # An unfilled `min_chars` means no minimum, in both functions; it must not become a
        # minimum of nothing, and it must not raise.
        {"min_chars": ""},
        {},
    ],
)
def test_the_mock_answer_satisfies_the_rubric_it_came_from(rubric: dict[str, Any]) -> None:
    """`_mock_answer` and `_score_answer` have to agree on what a rubric says.

    They did not. One read a key as present when it was not None, the other when it was truthy,
    so an item whose right answer was the empty string got scored against a rule nobody wrote.
    That is a property of every rubric shape rather than of the one that happened to break, and
    a key added to one function and not the other reproduces it exactly.

    The documented exceptions are the rubrics that could never say no — an empty `contains` and
    a negative `min_chars` — which fail closed by design and are pinned just below.
    """
    from felix.eval.runner import _mock_answer, _score_answer

    ok, score, _rule = _score_answer(_mock_answer(rubric), rubric)

    assert (ok, score) == (True, 1.0), rubric


def test_the_smoke_fixture_is_positive_by_construction() -> None:
    """The mirror of the guard below, for the half that is supposed to pass.

    Rewrite every smoke rubric to `{"mock_answer": "x"}` and the CI smoke step, `make check-ci`
    and both smoke assertions above all stay green — while the fixture stops exercising
    `equals`, `contains` and `min_chars` entirely. The pair only means something for as long as
    both halves reach the same rules.
    """
    from felix.eval.runner import _mock_answer, _score_answer

    payload = _fixture("smoke")
    assert payload["items"], "the smoke fixture is empty"

    rules = set()
    for item in payload["items"]:
        rubric = item["rubric"]
        ok, _score, rule = _score_answer(_mock_answer(rubric), rubric)
        assert ok is True, f"{item['item_id']} would fail; the smoke fixture must pass every item"
        rules.add(rule)

    # A subset by necessity, unlike the negative half: the rules that only ever reject —
    # `invalid_rubric` today — cannot appear in a fixture whose every item must pass.
    assert {"equals", "contains", "min_chars"} <= rules, rules


def test_the_negative_fixture_is_negative_by_construction() -> None:
    """Guards the fixture rather than the scorer.

    A `mock_answer` edited to satisfy its rubric would make the run above pass and quietly
    remove the only thing proving the gate can fail. `_mock_answer` returns `mock_answer`
    verbatim, so scoring each item's answer against its own rubric is exactly what the run does.
    """
    from felix.eval.runner import _mock_answer, _score_answer

    payload = _fixture("negative")
    assert payload["items"], "the negative fixture is empty"

    rules = set()
    for item in payload["items"]:
        rubric = item["rubric"]
        assert "mock_answer" in rubric, item
        ok, _score, rule = _score_answer(_mock_answer(rubric), rubric)
        assert ok is False, f"{item['item_id']} would pass; the fixture must fail every item"
        rules.add(rule)

    # Diversity, not just presence: items all rejected by one rule would satisfy every
    # assertion above while leaving the scorer's other branches unproven. Every rule the scorer
    # can return has to be a rule this fixture has seen it return.
    assert rules == _scorer_rule_names(), rules
