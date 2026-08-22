"""Comparative eval harnesses — baseline vs candidates with optional LLM judges."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from felix.config import Settings
from felix.eval.runner import _mock_answer, _score_answer, start_run

logger = logging.getLogger("felix.eval.compare")


@dataclass
class EvalHarness:
    """Named treatment for comparative evals."""

    name: str
    manifest: str
    model_id: str | None = None
    repetitions: int = 1
    mock: bool = False
    transform_system_prompt: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


def eval_harness_table(
    title: str,
    *,
    baseline: EvalHarness,
    candidate: EvalHarness | None = None,
    candidates: list[EvalHarness] | None = None,
    repetitions: int = 1,
) -> list[dict[str, Any]]:
    """Build a table of (name, harness, repetition) rows for comparative runs."""
    reps = max(1, int(repetitions))
    treatments = [baseline]
    if candidate is not None:
        treatments.append(candidate)
    if candidates:
        treatments.extend(candidates)
    rows: list[dict[str, Any]] = []
    for h in treatments:
        r = max(1, int(h.repetitions or reps))
        for i in range(r):
            rows.append(
                {
                    "title": title,
                    "name": h.name,
                    "repetition": i + 1,
                    "harness": h,
                    "is_baseline": h.name == baseline.name,
                }
            )
    return rows


def _input_key(item: dict[str, Any]) -> str:
    if item.get("item_id") or item.get("id"):
        return str(item.get("item_id") or item.get("id"))
    blob = json.dumps(item, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def pass_rate(scores: list[dict[str, Any]]) -> float:
    if not scores:
        return 0.0
    judged = [s for s in scores if "pass" in s or "score" in s]
    if not judged:
        return 0.0
    wins = sum(1 for s in judged if s.get("pass") or float(s.get("score") or 0) >= 1.0)
    return wins / len(judged)


def pass_rate_lift(baseline_scores: list[dict[str, Any]], candidate_scores: list[dict[str, Any]]) -> float:
    """Candidate pass rate minus baseline, in percentage points (0–100)."""
    return round((pass_rate(candidate_scores) - pass_rate(baseline_scores)) * 100, 2)


async def run_comparative(
    settings: Settings,
    *,
    tenant_id: str,
    dataset_name: str,
    baseline: EvalHarness,
    candidates: list[EvalHarness],
    tools: Any = None,
    judge_threshold: float | None = None,
    mock: bool = False,
) -> dict[str, Any]:
    """Run baseline + candidates on the same dataset; report lift.

    ``judge_threshold=None`` means observe only (do not fail the suite on low scores).
    """
    base_run = await start_run(
        settings,
        tools=tools,
        tenant_id=tenant_id,
        dataset_name=dataset_name,
        candidate_manifest=baseline.manifest,
        mock=mock or baseline.mock,
    )
    base_scores = list(base_run.get("scores") or [])
    results: list[dict[str, Any]] = [
        {
            "name": baseline.name,
            "manifest": baseline.manifest,
            "is_baseline": True,
            "pass_rate": pass_rate(base_scores),
            "run": base_run,
            "lift_pp": 0.0,
        }
    ]
    for cand in candidates:
        run = await start_run(
            settings,
            tools=tools,
            tenant_id=tenant_id,
            dataset_name=dataset_name,
            candidate_manifest=cand.manifest,
            mock=mock or cand.mock,
        )
        scores = list(run.get("scores") or [])
        lift = pass_rate_lift(base_scores, scores)
        entry = {
            "name": cand.name,
            "manifest": cand.manifest,
            "is_baseline": False,
            "pass_rate": pass_rate(scores),
            "run": run,
            "lift_pp": lift,
        }
        if judge_threshold is not None and pass_rate(scores) < judge_threshold:
            entry["below_threshold"] = True
        results.append(entry)

    return {
        "dataset": dataset_name,
        "baseline": baseline.name,
        "results": results,
        "judge_threshold": judge_threshold,
    }


async def llm_judge_score(
    model: Any,
    *,
    user_input: str,
    answer: str,
    criteria: str,
) -> dict[str, Any]:
    """Optional model-backed judge. Returns score in [0, 1]."""
    prompt = (
        f"Score the answer from 0.0 to 1.0 for this criteria.\n"
        f"Criteria: {criteria}\n"
        f"Question: {user_input}\n"
        f"Answer: {answer}\n"
        f"Reply with ONLY a JSON object: {{\"score\": 0.0, \"reason\": \"...\"}}"
    )
    try:
        from felix.patterns.types import ChatMessage

        result = await model.chat(
            [ChatMessage(role="user", content=prompt)],
            [],
        )
        text = (result.message.content or "").strip()
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            data = json.loads(text[start : end + 1])
            score = float(data.get("score") or 0)
            return {
                "score": max(0.0, min(1.0, score)),
                "reason": str(data.get("reason") or ""),
                "pass": score >= 1.0,
            }
    except Exception:
        logger.debug("llm judge failed", exc_info=True)
    # Fall back to nonempty heuristic
    ok, score, rule = _score_answer(answer, {"min_chars": 1})
    return {"score": score, "pass": ok, "rule": rule, "reason": "fallback"}


__all__ = [
    "EvalHarness",
    "eval_harness_table",
    "llm_judge_score",
    "pass_rate",
    "pass_rate_lift",
    "run_comparative",
]
