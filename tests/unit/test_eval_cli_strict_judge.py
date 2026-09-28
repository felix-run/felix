"""`felix eval` warns when a judge fell back to the heuristic, and `--strict-judge` fails on it.

The run's own result is patched in: what is under test is the CLI's decision — the exit code CI
reads and the warning on stderr — not the runner, which `tests/e2e/test_eval_instrumentation.py`
drives through the real app.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from typer.testing import CliRunner


def _run(monkeypatch: pytest.MonkeyPatch, fallbacks: int, *args: str) -> Any:
    from felix.eval import runner
    from felix_cli.main import app

    async def fake_start_run(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {"pass_count": 1, "fail_count": 0, "scores": [], "stats": {"judge_fallbacks": fallbacks}}

    monkeypatch.setattr(runner, "start_run", fake_start_run)
    return CliRunner().invoke(app, ["eval", "--dataset", "d", "--manifest", "quick", *args])


def test_a_fallback_warns_but_passes_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _run(monkeypatch, 1)
    assert result.exit_code == 0, result.output
    assert "scored by the heuristic" in result.stderr
    assert json.loads(result.stdout)["stats"]["judge_fallbacks"] == 1, "stdout stays the parseable run"


def test_strict_judge_fails_a_run_whose_judge_fell_back(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _run(monkeypatch, 1, "--strict-judge").exit_code == 1
    assert _run(monkeypatch, 0, "--strict-judge").exit_code == 0
