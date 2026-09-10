"""The eval gate has to be able to say no.

CI runs `felix eval --dataset smoke --fixture fixtures/eval/smoke.json --mock` and treats a
non-zero exit as a failure. Every item in that fixture carries a `mock_answer` that satisfies
its own rubric, and `_mock_answer` returns it verbatim — so the run passes by construction. A
scorer rewritten to `return True, 1.0, "x"` would leave that step green, and so would one that
never ran at all.

This is the counter-smoke: a fixture whose every item must fail, and the assertion that it does.
Together the two say the gate can distinguish a right answer from a wrong one, which neither
says alone.

It runs here as well as in CI, because a gate that only exists in a workflow file is one nobody
can check before pushing.
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


async def _run_fixture(name: str) -> dict[str, Any]:
    """Load a bundled fixture and score it exactly as `felix eval --mock` does."""
    settings = _settings()
    payload = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))

    await eval_store.put_dataset(
        settings,
        "default",
        payload["name"],
        description=payload.get("description", ""),
        items=payload["items"],
    )
    return await start_run(
        settings,
        tools=None,
        tenant_id="default",
        dataset_name=payload["name"],
        candidate_manifest="quick",
        mock=True,
        deterministic_judge=True,
    )


@pytest.mark.asyncio
async def test_the_smoke_fixture_passes_every_item() -> None:
    """One half of the pair, and the half CI already had."""
    result = await _run_fixture("smoke")

    assert result["fail_count"] == 0, result
    assert result["pass_count"] == 3, result


@pytest.mark.asyncio
async def test_the_negative_fixture_fails_every_item() -> None:
    """The other half: a scorer that always passes turns this red and the smoke run green."""
    result = await _run_fixture("negative")

    assert result["pass_count"] == 0, result
    assert result["fail_count"] == 3, result


@pytest.mark.asyncio
async def test_each_scoring_rule_is_exercised_in_both_directions() -> None:
    """Per rule, not just in aggregate.

    Three passes and three failures could both come from one rule while the other two were
    never reached — which is what a `contains`-only scorer would look like.
    """
    from felix.eval.runner import _score_answer

    cases = [
        ({"expect": "ok"}, "ok", "not ok", "equals"),
        ({"contains": "hello"}, "well hello there", "goodbye", "contains"),
        ({"min_chars": 32}, "x" * 32, "tiny", "min_chars"),
        ({}, "anything", "", "nonempty"),
    ]
    for rubric, good, bad, rule in cases:
        ok_good, score_good, name_good = _score_answer(good, rubric)
        ok_bad, score_bad, name_bad = _score_answer(bad, rubric)

        assert (ok_good, score_good, name_good) == (True, 1.0, rule), (rubric, good)
        assert (ok_bad, score_bad, name_bad) == (False, 0.0, rule), (rubric, bad)


def test_the_negative_fixture_is_negative_by_construction() -> None:
    """Guards the fixture rather than the scorer.

    A `mock_answer` edited to satisfy its rubric would make the run above pass and quietly
    remove the only thing proving the gate can fail. `_mock_answer` returns `mock_answer`
    verbatim, so scoring each item's answer against its own rubric is exactly what the run does.
    """
    from felix.eval.runner import _mock_answer, _score_answer

    payload = json.loads((FIXTURES / "negative.json").read_text(encoding="utf-8"))
    assert payload["items"], "the negative fixture is empty"

    for item in payload["items"]:
        rubric = item["rubric"]
        assert "mock_answer" in rubric, item
        ok, _score, _rule = _score_answer(_mock_answer(rubric), rubric)
        assert ok is False, f"{item['item_id']} would pass; the fixture must fail every item"
