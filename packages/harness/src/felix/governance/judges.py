"""Judge scoring: the heuristic rubric and the model-backed scorer.

Used by the tool judges in `manifests/builder.py`, the reply-path controls in
`governance/reply.py`, and the delegating pattern's self-check — one scorer, so a new
rubric prefix or a new provider is added in one place, and governance does not have to
reach into the compile pipeline for a private name.
"""

from __future__ import annotations

import logging
from typing import Any

from felix.manifests.schema import JudgeRule

logger = logging.getLogger("felix.governance.judges")


def heuristic_judge_score(content: str, criteria: str) -> float:
    """Score tool/final output against judge criteria (0..1).

    Supports:
    * nonempty / not empty
    * min_length:N / min_chars:N
    * keyword overlap with criteria tokens (default)
    """
    text = (content or "").strip()
    c = (criteria or "").strip().lower()
    if not c:
        return 1.0 if len(text) >= 3 else 0.0
    if "nonempty" in c or "not empty" in c or c in {"relevance", "useful"}:
        return 1.0 if len(text) >= 3 else 0.0
    for prefix in ("min_length:", "min_chars:"):
        if c.startswith(prefix):
            try:
                n = max(int(c.split(":", 1)[1].strip()), 1)
            except ValueError:
                n = 1
            return min(1.0, len(text) / n)
    # Explicit assertions, so a criterion's *polarity* is stated rather than inferred.
    for prefix in ("assert_absent:", "must_not_contain:"):
        if c.startswith(prefix):
            needles = [n.strip() for n in c.split(":", 1)[1].split(",") if n.strip()]
            lower = text.lower()
            return 0.0 if any(n in lower for n in needles) else 1.0
    for prefix in ("assert_present:", "must_contain:"):
        if c.startswith(prefix):
            needles = [n.strip() for n in c.split(":", 1)[1].split(",") if n.strip()]
            lower = text.lower()
            return 1.0 if all(n in lower for n in needles) else 0.0

    # Bag-of-words overlap cannot express a negative criterion: for
    # "must not leak credentials or secrets" it scored output *containing* those words
    # highest, so a safety judge passed exactly what it was meant to block. Refuse to
    # guess — a judge with no model and no explicit assertion fails closed.
    if _looks_negated(c):
        logger.error(
            "judge criteria %r is a negative assertion but has no model and no "
            "assert_absent: prefix; failing closed",
            criteria,
        )
        return 0.0

    tokens = [t for t in c.replace(",", " ").split() if len(t) > 2]
    if not tokens:
        return 1.0 if len(text) >= 3 else 0.0
    lower = text.lower()
    words = set(lower.split())
    hit = sum(1 for t in tokens if t in words or t in lower)
    return hit / len(tokens)


_NEGATION_MARKERS = (
    "must not",
    "should not",
    "never",
    "no ",
    "without",
    "avoid",
    "free of",
    "does not",
    "doesn't",
    "don't",
    "cannot",
    "refuse",
    "prohibit",
    "forbid",
)


def _looks_negated(criteria: str) -> bool:
    """True when a criterion reads as "must NOT ..." — polarity a bag of words inverts."""
    return any(m in criteria for m in _NEGATION_MARKERS)


async def judge_score(content: str, judge: JudgeRule, *, settings: Any | None = None) -> float:
    """Heuristic score, or LLM score when ``judge.model`` is set."""
    criteria = judge.criteria
    model_id = judge.model.strip()
    if not model_id or settings is None:
        return heuristic_judge_score(content, criteria)
    try:
        from felix.eval.compare import llm_judge_score
        from felix.manifests.schema import ModelSpec
        from felix.patterns.model import build_model

        model = build_model(settings, ModelSpec(id=model_id))
        result = await llm_judge_score(
            model,
            user_input="",
            answer=content,
            criteria=criteria,
            threshold=judge.threshold,
        )
        return float(result.get("score") or 0.0)
    except Exception:
        return heuristic_judge_score(content, criteria)


__all__ = ["heuristic_judge_score", "judge_score"]
