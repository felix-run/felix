"""`felix.eval.validation`, and the two paths that have to call it.

The validator's whole purpose is to turn a silent success into a loud failure, so the tests
that matter are the ones driving the real write paths — `PUT /eval/datasets/{name}` and
`felix eval --fixture`. A validator nothing calls would pass every test in this file's first
half and change nothing for anyone.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest
from felix.config import Settings
from felix.eval import store as eval_store
from felix.eval.validation import RUBRIC_RULE_KEYS, read_item, validate_items

ROOT = pathlib.Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "fixtures" / "eval"


@pytest.fixture(autouse=True)
def _isolate_process_settings() -> Any:
    """The CLI stamps a process role onto the cached `Settings` and loads plugins."""
    from felix.config import get_settings

    yield
    get_settings.cache_clear()


def _settings() -> Settings:
    return Settings(database_url="memory://eval-validation", object_store="memory")


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- the rules


def test_the_spelling_that_scores_nothing_is_rejected_by_name() -> None:
    """`input` instead of `user_input` is the case this exists for.

    Felix's own bundled fixtures once used it, and `put_dataset` stores such an item with an
    empty prompt — so the message has to name the key that was found, not only the one that
    is missing.
    """
    report = validate_items([{"item_id": "a", "input": "Say hello", "expect": "hello"}])

    assert not report.ok
    assert any("user_input" in e and "'input'" in e for e in report.errors), report.errors


@pytest.mark.parametrize("alias", ["input", "prompt", "question", "query", "user_message", "text"])
def test_every_near_miss_key_is_named_back(alias: str) -> None:
    report = validate_items([{"item_id": "a", alias: "Say hello"}])

    assert not report.ok
    assert any(repr(alias) in e for e in report.errors), report.errors


def test_every_problem_is_reported_at_once() -> None:
    """One pass, whole list — someone fixing a hand-written dataset needs all of it."""
    report = validate_items(
        [
            {"item_id": "dup", "user_input": "a", "rubric": {"contains": "x"}},
            {"item_id": "dup", "user_input": "b", "rubric": {"contains": "x"}},
            {"item_id": "bad-rubric", "user_input": "c", "rubric": "contains: x"},
            "not even an object",
        ]
    )

    joined = " | ".join(report.errors)
    assert "repeats item_id 'dup'" in joined
    assert "bad-rubric" in joined and "rubric is str" in joined
    assert "items[3] is str" in joined


def test_the_validator_reads_an_item_exactly_as_put_dataset_does() -> None:
    """The two disagreeing is the defect this shares a reader to prevent.

    Both spellings of the rubric key, and a falsy `item_id` meaning *absent* rather than an
    id of its own — `put_dataset` does `item.get("item_id") or uuid4().hex`.
    """
    assert read_item({"rubric_json": {"contains": "x"}}).rubric == {"contains": "x"}
    assert read_item({"rubric": {"contains": "a"}, "rubric_json": {"contains": "b"}}).rubric == {
        "contains": "a"
    }
    assert read_item({"item_id": ""}).item_id is None
    assert read_item({"item_id": 7}).item_id == "7"


@pytest.mark.parametrize("rubric_key", ["rubric", "rubric_json"])
def test_both_rubric_spellings_are_judged_the_same(rubric_key: str) -> None:
    """`put_dataset` and the runner both read `rubric` *or* `rubric_json`.

    The validator once read only the first, so a rubric under the other spelling drew a
    false "names no rule" warning while being stored and scored correctly — and a malformed
    one under that spelling sailed through the gate that exists to refuse it.
    """
    assert (
        validate_items([{"item_id": "a", "user_input": "hi", rubric_key: {"contains": "x"}}]).warnings == []
    )

    bad = validate_items([{"item_id": "a", "user_input": "hi", rubric_key: "not-a-mapping"}])
    assert not bad.ok, bad
    assert any("rubric is str" in e for e in bad.errors), bad.errors


def test_an_empty_item_id_is_absent_rather_than_a_duplicate() -> None:
    """`put_dataset` mints a uuid for each, so these are two items, not a collision.

    Treating `""` as an id refused a batch the store handles perfectly well — a validator
    rejecting valid input is worse than the silence it replaced.
    """
    report = validate_items([{"item_id": "", "user_input": "one"}, {"item_id": "", "user_input": "two"}])

    assert report.ok, report.errors


@pytest.mark.parametrize(
    "rubric",
    [
        {"llm_judge": True},
        {"judge_criteria": "is polite"},
        {"judge_model": "claude-haiku"},
        {"criteria": "is polite"},
        {"judge_threshold": 0.9},
        {"llm_judge": False},
        {},
    ],
)
def test_the_warning_agrees_with_the_runner_about_what_selects_a_judge(rubric: dict[str, Any]) -> None:
    """The constant that listed judge keys was wider than the dispatch that reads them.

    `criteria` and `judge_threshold` only *tune* a judge — `_wants_llm_judge` never looks at
    them — so a rubric carrying one scored as `nonempty`, passing any answer, with the
    warning that exists for exactly that case suppressed. Asking the runner directly is what
    makes the two unable to drift.
    """
    from felix.eval.runner import _wants_llm_judge

    judged = _wants_llm_judge(rubric, deterministic_judge=False)
    warned = bool(validate_items([{"item_id": "a", "user_input": "hi", "rubric": rubric}]).warnings)

    assert warned is not judged, f"rubric={rubric} judged={judged} warned={warned}"


def test_a_rubric_that_only_tunes_a_judge_says_so() -> None:
    """Generic advice would send the author looking in the wrong place."""
    report = validate_items(
        [{"item_id": "a", "user_input": "hi", "rubric": {"criteria": "polite", "judge_threshold": 0.9}}]
    )

    assert report.ok
    assert "nothing selects a judge" in report.warnings[0], report.warnings
    assert "criteria, judge_threshold" in report.warnings[0], report.warnings


def test_the_rule_key_list_matches_what_the_scorer_dispatches_on() -> None:
    """Read off `_score_answer`'s source, so a new rule cannot land without this knowing.

    A hardcoded list of what another function reads is the thing that rots — the same
    argument `test_eval_gate_can_fail._scorer_rule_names` makes for the rule *names*.
    """
    import ast
    import inspect

    from felix.eval.runner import _score_answer

    tree = ast.parse(inspect.getsource(_score_answer).strip())
    dispatched = {
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "rubric"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    }

    assert dispatched, "could not read any rubric.get(...) key off _score_answer"
    assert dispatched == set(RUBRIC_RULE_KEYS), (
        f"_score_answer dispatches on {sorted(dispatched)}, RUBRIC_RULE_KEYS says {sorted(RUBRIC_RULE_KEYS)}"
    )


def test_a_ruleless_rubric_warns_rather_than_rejects() -> None:
    """It is a real rule — `nonempty` — and it passes any answer at all."""
    report = validate_items([{"item_id": "a", "user_input": "hi", "rubric": {}}])

    assert report.ok
    assert any("non-empty" in w for w in report.warnings), report.warnings


def test_a_rubric_of_judge_settings_alone_is_not_ruleless() -> None:
    """The judge replaces the heuristic, so naming one is naming a rule."""
    report = validate_items([{"item_id": "a", "user_input": "hi", "rubric": {"judge_criteria": "is polite"}}])

    assert report.ok
    assert not report.warnings, report.warnings


def test_an_empty_item_list_warns() -> None:
    report = validate_items([])

    assert report.ok
    assert any("scores nothing" in w for w in report.warnings), report.warnings


def test_items_that_are_not_a_list_are_rejected() -> None:
    assert not validate_items({"item_id": "a"}).ok
    assert not validate_items(None).ok


@pytest.mark.parametrize("name", ["smoke", "negative"])
def test_the_bundled_fixtures_are_never_refused(name: str) -> None:
    """The gate CI runs must not be refused by the gate this adds.

    `negative.json` exists to fail *scoring* — every item has to be well-formed enough to be
    scored down, which is what this asserts. It is the fixture most likely to trip a new
    rule, because it is built out of rubrics that reject.
    """
    assert validate_items(_fixture(name)["items"]).ok


def test_the_smoke_fixture_is_clean_and_the_negative_one_warns_where_it_means_to() -> None:
    """`negative.json`'s `empty-answer` item carries `{"mock_answer": ""}` and no rule.

    That is deliberate — it exists to watch the non-empty rule reject a blank answer — so the
    warning is correct rather than something to silence. Pinned so that a *new* ruleless item
    appearing in either fixture is noticed instead of blending into an expected warning.
    """
    assert validate_items(_fixture("smoke")["items"]).warnings == []

    warned = validate_items(_fixture("negative")["items"]).warnings
    assert len(warned) == 1, warned
    assert "empty-answer" in warned[0], warned


# --------------------------------------------------------------------------- the write paths


@pytest.mark.asyncio
async def test_the_put_route_refuses_and_writes_nothing() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from felix_api.routes import eval as eval_routes

    app = FastAPI()
    app.state.settings = _settings()
    app.include_router(eval_routes.router, prefix="/eval")
    client = TestClient(app)

    refused = client.put("/eval/datasets/mistyped", json={"items": [{"input": "2+2?"}]})

    assert refused.status_code == 422, refused.text
    detail = refused.json()["detail"]
    # This endpoint returns 422 twice over — pydantic's own list-shaped detail fires for a
    # body failing `extra="forbid"` — so the code is what tells a client which one this is.
    assert detail["code"] == "eval_items_invalid", detail
    assert any("user_input" in e for e in detail["errors"])
    # A refused write that half-applied would be worse than the behaviour it replaced.
    assert await eval_store.get_dataset(_settings(), "default", "mistyped") is None


@pytest.mark.asyncio
async def test_the_put_route_returns_warnings_with_the_stored_dataset() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from felix_api.routes import eval as eval_routes

    app = FastAPI()
    app.state.settings = _settings()
    app.include_router(eval_routes.router, prefix="/eval")
    client = TestClient(app)

    created = client.put(
        "/eval/datasets/loose", json={"items": [{"item_id": "a", "user_input": "hi", "rubric": {}}]}
    )

    assert created.status_code == 200, created.text
    assert any("non-empty" in w for w in created.json()["warnings"]), created.json()
    stored = await eval_store.get_dataset(_settings(), "default", "loose")
    assert stored is not None and len(stored["items"]) == 1

    # Present and empty when there is nothing to say, rather than absent: one shape for the
    # caller either way.
    clean = client.put(
        "/eval/datasets/tight",
        json={"items": [{"item_id": "a", "user_input": "hi", "rubric": {"contains": "h"}}]},
    )
    assert clean.json()["warnings"] == [], clean.json()


def test_the_cli_refuses_a_bad_fixture_with_exit_2(tmp_path: pathlib.Path) -> None:
    """Exit 2, not 1.

    CI reads exit 1 as "the eval ran and items failed" — `scripts/eval-counter-smoke.sh`
    depends on that meaning. A malformed dataset is a usage error and must not be mistaken
    for a scored failure.
    """
    from felix_cli.main import app as cli_app
    from typer.testing import CliRunner

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"name": "bad", "items": [{"item_id": "a", "input": "2+2?"}]}))

    result = CliRunner().invoke(
        cli_app, ["eval", "--dataset", "bad", "--manifest", "quick", "--fixture", str(bad), "--mock"]
    )

    assert isinstance(result.exception, SystemExit), result.exception
    assert result.exception.code == 2, result.exception
    assert "user_input" in result.output


def test_the_cli_prints_a_warning_and_still_runs(tmp_path: pathlib.Path) -> None:
    """Deleting the warning loop in the CLI left the whole suite green.

    A ruleless rubric is legal, so the run proceeds and exits 0 — which means the only
    evidence the author gets that their dataset gates nothing is this line on stderr.
    """
    from felix_cli.main import app as cli_app
    from typer.testing import CliRunner

    loose = tmp_path / "loose.json"
    loose.write_text(
        json.dumps(
            {"name": "loose", "items": [{"item_id": "a", "user_input": "hi", "rubric": {"mock_answer": "x"}}]}
        )
    )

    result = CliRunner().invoke(
        cli_app, ["eval", "--dataset", "loose", "--manifest", "quick", "--fixture", str(loose), "--mock"]
    )

    assert result.exception is None, result.exception
    assert "names no rule" in result.output, result.output


def test_the_cli_still_runs_a_good_fixture() -> None:
    """The invocation the Makefile and the CI eval job use, unchanged."""
    from felix_cli.main import app as cli_app
    from typer.testing import CliRunner

    result = CliRunner().invoke(
        cli_app,
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

    assert result.exception is None, result.exception
    assert json.loads(result.stdout)["pass_count"] == len(_fixture("smoke")["items"])
