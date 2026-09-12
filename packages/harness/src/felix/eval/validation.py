"""Reject an eval dataset item that would be stored and then score nothing.

Every write path takes free-form dicts, so a near-miss key is accepted, stored with an
empty prompt, and scored by a rule its author never wrote. The dataset looks configured,
the run completes, and nothing was ever asked. That failure is silent at every layer
below this one:

* `put_dataset` stores `user_input` as `""` when the key is absent or misspelled — Felix's
  own bundled fixtures once used `input`, which is exactly the spelling that produces it.
* `_score_answer` falls through to the `nonempty` rule when a rubric names none of
  `expect` / `equals` / `contains` / `min_chars`, and `nonempty` passes any answer that is
  not blank.

Warnings are for an item that is legal and probably not intended; errors are for one that
cannot do its job. Both are collected for the whole batch rather than raised on the first,
because someone fixing a hand-written dataset needs the whole list.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

# The rubric keys `felix.eval.runner._score_answer` dispatches on, in its own precedence
# order. A rubric carrying none of these is not invalid — it scores as `nonempty`.
# `test_eval_item_validation.py` reads the same set off `_score_answer`'s source, so a new
# rule fails there until this list knows about it.
RUBRIC_RULE_KEYS = ("expect", "equals", "contains", "min_chars")

# Keys `_maybe_llm_judge` reads to *tune* a judge. They do not select one — `_wants_llm_judge`
# looks at `llm_judge` / `judge_criteria` / `judge_model` and nothing else — so a rubric
# carrying only these scores as `nonempty` while looking judged. That is worth saying out
# loud rather than treating as a rule. Whether a judge is selected is asked of the runner
# itself rather than re-listed here, because a list is the thing that drifts.
RUBRIC_JUDGE_TUNING_KEYS = ("criteria", "judge_threshold")

# Near-misses for `user_input`, named back to the author so the message says what to
# change rather than only what is missing.
USER_INPUT_ALIASES = ("input", "prompt", "question", "query", "user_message", "text")


@dataclass(frozen=True, slots=True)
class ItemFields:
    """One reading of an eval item, shared by the validator and `put_dataset`.

    The two disagreeing is not hypothetical: the validator once read `item["rubric"]` while
    the store read `rubric` *or* `rubric_json`, and treated `item_id: ""` as an id while the
    store treats it as absent and mints a uuid. The first produced a warning about a rubric
    that was stored and scored correctly; the second refused, with a duplicate-id error, a
    batch the store handles fine.
    """

    item_id: str | None
    user_input: Any
    rubric: Any


def read_item(item: Mapping[str, Any]) -> ItemFields:
    """Read an item exactly as `felix.eval.store.put_dataset` reads it."""
    raw_id = item.get("item_id")
    return ItemFields(
        # Falsy means absent: `put_dataset` does `item.get("item_id") or uuid4().hex`.
        item_id=str(raw_id) if raw_id else None,
        user_input=item.get("user_input", ""),
        rubric=item.get("rubric") or item.get("rubric_json") or {},
    )


@dataclass(frozen=True, slots=True)
class ValidationReport:
    """Everything wrong with a batch of items, reported at once."""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def validate_items(items: Any) -> ValidationReport:
    """Check dataset items before they reach `put_dataset`."""
    from felix.eval.runner import _wants_llm_judge

    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(items, list):
        return ValidationReport([f"items is {type(items).__name__}, expected a list"], [])
    if not items:
        warnings.append("items is empty — a run over this dataset scores nothing")

    seen: set[str] = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            errors.append(f"items[{index}] is {type(item).__name__}, expected an object")
            continue

        fields = read_item(item)
        # Lead with the id once there is one. An index alone sends the reader counting array
        # elements in a file they did not write. Kept separate from the field paths below, so
        # a message about `rubric` does not read as `items[0]` having a `.rubric`.
        label = f"items[{index}]" if fields.item_id is None else f"item {fields.item_id!r} (items[{index}])"
        if fields.item_id is not None:
            if fields.item_id in seen:
                errors.append(
                    f"{label} repeats item_id {fields.item_id!r}; the later item would win silently"
                )
            seen.add(fields.item_id)

        if not isinstance(fields.user_input, str) or not fields.user_input.strip():
            alias = next((k for k in USER_INPUT_ALIASES if k in item), None)
            hint = f" (found {alias!r} — the key Felix reads is 'user_input')" if alias else ""
            errors.append(f"{label} has no user_input{hint}; it would be stored as an empty prompt")

        rubric = fields.rubric
        if not isinstance(rubric, dict):
            errors.append(
                f"{label} rubric is {type(rubric).__name__}, expected an object; the run would error"
            )
            continue
        if any(k in rubric for k in RUBRIC_RULE_KEYS):
            continue
        # Asked of the runner rather than re-derived, so this cannot drift from the dispatch
        # it describes. `deterministic_judge=False` is the permissive reading: if no judge is
        # selected even then, none ever will be.
        if _wants_llm_judge(rubric, deterministic_judge=False):
            continue
        tuning = [k for k in RUBRIC_JUDGE_TUNING_KEYS if k in rubric]
        detail = (
            f"names judge settings ({', '.join(tuning)}) but nothing selects a judge — "
            "that needs llm_judge, judge_criteria or judge_model"
            if tuning
            else f"names no rule ({', '.join(RUBRIC_RULE_KEYS)}) and no judge"
        )
        warnings.append(
            f"{label} rubric {detail}; it will score as non-empty, which passes any answer at all"
        )

    return ValidationReport(errors, warnings)


__all__ = [
    "RUBRIC_JUDGE_TUNING_KEYS",
    "RUBRIC_RULE_KEYS",
    "USER_INPUT_ALIASES",
    "ItemFields",
    "ValidationReport",
    "read_item",
    "validate_items",
]
