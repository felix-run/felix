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
from typing import Any

import pytest
from felix.config import Settings
from felix.eval import store as eval_store
from felix.eval.runner import start_run

FIXTURES = pathlib.Path(__file__).resolve().parents[2] / "fixtures" / "eval"


def _settings() -> Settings:
    return Settings(database_url="memory://eval-gate", object_store="memory")


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


async def _run_fixture(name: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Load a bundled fixture and score it exactly as `felix eval --mock` does.

    Returns the completed run alongside the fixture's items, so counts are asserted against
    the fixture's own length — adding an item should never turn one of these red on its own.
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
    assert {row["rule"] for row in rows} == {"equals", "contains", "min_chars", "nonempty"}, rows


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

    assert result.exit_code == 1, result.output
    assert "fail_count" in result.output, result.output


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

    for answer in ("anything at all", ""):
        assert _score_answer(answer, {"contains": ""}) == (False, 0.0, "invalid_rubric"), answer


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

    # Diversity, not just presence: five items all rejected by `contains` would satisfy every
    # assertion above while leaving three of the scorer's four branches unproven.
    assert rules == {"equals", "contains", "min_chars", "nonempty"}, rules
