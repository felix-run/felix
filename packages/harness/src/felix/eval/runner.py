"""Offline eval runner — score dataset items against a candidate manifest."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from felix.config import Settings
from felix.context import AuthContext, RequestContext, async_run_with_context
from felix.eval import store as eval_store
from felix.logging_setup import loggable
from felix.patterns.types import ChatMessage, InvokeInput
from felix.runtime import build_tenant_agent, resolve_tenant_manifest
from felix.thread_ids import eval_thread_id
from felix.tools.types import is_failure_content

logger = logging.getLogger("felix.eval.runner")


@dataclass(frozen=True, slots=True)
class Trajectory:
    """What the run *did*, as opposed to what it said.

    `tool_names` in call order, one entry per tool invocation; `errors` is how many of those
    came back as a tool error (`[error/...]`, `[fatal/...]`) or a governance denial.
    """

    tool_names: tuple[str, ...] = ()
    errors: int = 0


def trajectory_of(messages: list[ChatMessage]) -> Trajectory:
    """Read the trajectory off the messages a run produced.

    A tool message carries only text by the time it is here — the deny and error markers on
    the `ToolOutput` do not survive into a `ChatMessage` — so a failure is recognised by the
    spelling every producer uses, `FAILURE_CONTENT_PREFIXES`, which lives beside `deny_output`.
    """
    names: list[str] = []
    errors = 0
    for m in messages:
        if m.role == "assistant" and m.tool_calls:
            names.extend(tc.name for tc in m.tool_calls)
        elif m.role == "tool" and is_failure_content(m.content):
            errors += 1
    return Trajectory(tool_names=tuple(names), errors=errors)


_INVALID = object()


def _ceiling(raw: Any) -> int | object | None:
    """An integer ceiling from a rubric value: None when absent, `_INVALID` when unscoreable.

    The shape `min_chars` had already worked out — `""` means no rule, a value `int()` rejects
    is a rubric nobody can score, a negative one could never say no — written once so the next
    ceiling rule does not re-remember `OverflowError`.
    """
    if raw is None or raw == "":
        return None
    try:
        value = int(raw)
    except TypeError, ValueError, OverflowError:
        return _INVALID
    return _INVALID if value < 0 else value


def _mock_trajectory(rubric: dict[str, Any]) -> Trajectory:
    """The trajectory `--mock` scores, the way `mock_answer` is the answer it scores.

    Without it a trajectory rule could only ever pass under `--mock` (no tool ran), and the
    counter-smoke could never show one saying no.
    """
    raw = rubric.get("mock_tool_calls") or []
    names = tuple(str(n) for n in raw) if isinstance(raw, list) else ()
    try:
        errors = int(rubric.get("mock_tool_errors") or 0)
    except TypeError, ValueError, OverflowError:
        errors = 0
    return Trajectory(tool_names=names, errors=errors)


def _score_answer(
    answer: str, rubric: dict[str, Any], trajectory: Trajectory | None = None
) -> tuple[bool, float, str]:
    """Heuristic scorer — trajectory rules, then expects / contains / min_chars.

    Trajectory rules (`tools_called`, `tools_not_called`, `max_tool_calls`, `max_errors`) are
    read first and can only *reject*: an item that names one and an answer rule needs both,
    and one that names only a trajectory rule falls through to the answer rules once the
    trajectory is right. A trajectory rule that could never reject — an empty list, a negative
    ceiling — is `invalid_rubric`, for the reason `contains: ""` is.

    `expect`, `equals` and `contains` count as present when they are not None, which is how
    `_mock_answer` already reads them. Reading them with `or` instead meant `{"expect": ""}`
    fell through to the non-empty check and scored the item against a rule its author never
    wrote, while `_mock_answer` cheerfully produced the empty answer that rubric asked for.

    `min_chars` is the exception in both functions: 0 and "" mean no minimum rather than a
    minimum of nothing, so they fall through to the non-empty rule.

    The rule for an empty value, which a new scoring rule should follow: honour it when the rule
    still discriminates (`{"expect": ""}` asks for an empty answer and rejects every other one),
    and return `invalid_rubric` when it would match everything (`{"contains": ""}`, a negative
    `min_chars`). The second kind is a rubric that could never say no, and it fails in the
    direction that hides problems — so it fails closed instead.
    """
    traj = trajectory or Trajectory()
    called = rubric.get("tools_called")
    if called is not None:
        if not isinstance(called, list) or not called:
            # Every trajectory contains every tool in an empty list.
            return False, 0.0, "invalid_rubric"
        missing = [str(t) for t in called if str(t) not in traj.tool_names]
        if missing:
            return False, 0.0, "tools_called"
    not_called = rubric.get("tools_not_called")
    if not_called is not None:
        if not isinstance(not_called, list) or not not_called:
            return False, 0.0, "invalid_rubric"
        seen = [str(t) for t in not_called if str(t) in traj.tool_names]
        if seen:
            return False, 0.0, "tools_not_called"
    max_calls = _ceiling(rubric.get("max_tool_calls"))
    if max_calls is _INVALID:
        return False, 0.0, "invalid_rubric"
    if isinstance(max_calls, int) and len(traj.tool_names) > max_calls:
        return False, 0.0, "max_tool_calls"
    max_errors = _ceiling(rubric.get("max_errors"))
    if max_errors is _INVALID:
        return False, 0.0, "invalid_rubric"
    if isinstance(max_errors, int) and traj.errors > max_errors:
        return False, 0.0, "max_errors"
    expect = rubric.get("expect")
    if expect is None:
        expect = rubric.get("equals")
    if expect is not None:
        ok = answer.strip() == str(expect).strip()
        return ok, 1.0 if ok else 0.0, "equals"
    contains = rubric.get("contains")
    if contains is not None:
        needle = str(contains)
        if not needle.strip():
            # Every answer contains the empty string, so this rubric could never say no —
            # an unfilled field far more often than an intent. Passing everything is the
            # failure direction that hides problems, so it fails closed instead.
            return False, 0.0, "invalid_rubric"
        ok = needle.lower() in answer.lower()
        return ok, 1.0 if ok else 0.0, "contains"
    min_chars_raw = rubric.get("min_chars")
    if min_chars_raw is not None and min_chars_raw != "":
        try:
            min_chars = int(min_chars_raw)
        except TypeError, ValueError, OverflowError:
            # A rubric nobody can score. Raising here would make the item an *error* rather
            # than a failure, and an errored item is indistinguishable from a rejected one in
            # the counts — so a malformed dataset would read as a working gate.
            return False, 0.0, "invalid_rubric"
        if min_chars < 0:
            # `len(answer) >= -1` holds for every answer, the empty one included — the same
            # rubric-that-cannot-reject as an empty `contains`, one branch down.
            return False, 0.0, "invalid_rubric"
        if min_chars:
            ok = len(answer.strip()) >= min_chars
            return ok, 1.0 if ok else 0.0, "min_chars"
    # Default: non-empty answer passes.
    ok = bool(answer.strip())
    return ok, 1.0 if ok else 0.0, "nonempty"


def _wants_llm_judge(rubric: dict[str, Any], *, deterministic_judge: bool) -> bool:
    if deterministic_judge:
        return False
    if rubric.get("llm_judge") is False:
        return False
    return bool(rubric.get("llm_judge") or rubric.get("judge_criteria") or rubric.get("judge_model"))


async def _maybe_llm_judge(
    settings: Settings,
    *,
    user_input: str,
    answer: str,
    rubric: dict[str, Any],
    heuristic: tuple[bool, float, str],
) -> dict[str, Any]:
    ok, score, rule = heuristic
    criteria = str(rubric.get("judge_criteria") or rubric.get("criteria") or "relevance")
    threshold = float(rubric.get("judge_threshold") or 0.7)
    # Defaulting to an Ollama route meant the judge silently degraded to the heuristic
    # on any deployment without a local model — see MemoryCapture.model.
    model_id = str(rubric.get("judge_model") or "claude-haiku")
    try:
        from felix.eval.compare import llm_judge_score
        from felix.manifests.schema import ModelSpec
        from felix.patterns.model import build_model

        model = build_model(settings, ModelSpec(id=model_id))
        judged = await llm_judge_score(
            model,
            user_input=user_input,
            answer=answer,
            criteria=criteria,
            threshold=threshold,
        )
        return {
            "pass": bool(judged.get("pass")),
            "score": float(judged.get("score") or 0),
            "rule": str(judged.get("rule") or "llm_judge"),
            "reason": str(judged.get("reason") or ""),
            "heuristic_pass": ok,
            "heuristic_score": score,
            "heuristic_rule": rule,
        }
    except Exception as exc:
        logger.debug("llm_judge unavailable: %s", exc, exc_info=True)
        return {
            "pass": ok,
            "score": score,
            "rule": rule,
            "reason": f"llm_fallback:{exc}",
        }


async def start_run(
    settings: Settings,
    *,
    tools: Any = None,
    tenant_id: str,
    dataset_name: str,
    candidate_manifest: str,
    manifest_version: int | None = None,
    mock: bool = False,
    deterministic_judge: bool = False,
    use_llm_judge: bool = False,
) -> dict[str, Any]:
    dataset = await eval_store.get_dataset(settings, tenant_id, dataset_name)
    items = (dataset or {}).get("items") or []

    run = await eval_store.create_run(
        settings,
        tenant_id=tenant_id,
        dataset_name=dataset_name,
        candidate_manifest=candidate_manifest,
        manifest_version=manifest_version,
    )

    if not items:
        completed = await eval_store.complete_run(
            settings,
            tenant_id,
            run["id"],
            pass_count=0,
            fail_count=0,
            scores=[],
        )
        return completed or run

    if tools is None and not mock:
        from felix.tools.builtins import default_tool_provider

        tools = default_tool_provider()

    auth = AuthContext(tenant_id=tenant_id, principal_sub="eval", anonymous=False)
    scores: list[dict[str, Any]] = []
    passes = 0
    fails = 0
    errors = 0

    resolved = None
    if not mock:
        try:
            # The version is recorded on the run row, so it has to be the version scored.
            # Without this the run reported a canary and measured whatever was active.
            resolved = await resolve_tenant_manifest(
                settings, tenant_id, candidate_manifest, pin_version=manifest_version
            )
        except Exception as exc:
            logger.exception("eval_resolve_failed")
            completed = await eval_store.complete_run(
                settings,
                tenant_id,
                run["id"],
                pass_count=0,
                fail_count=len(items),
                # Every one of them raised before it could be scored.
                error_count=len(items),
                scores=[{"error": str(exc)}],
            )
            return completed or run

    for item in items:
        item_id = str(item.get("item_id") or item.get("id") or "")
        user_input = str(item.get("user_input") or "")
        try:
            # `item_id` is dataset content, and `PUT /eval/datasets/{name}` takes the items
            # from the caller — so it is caller-supplied in the same way an A2A `taskId` is,
            # and it lands in the `session_events` primary key. Composed rather than
            # interpolated for that reason; see `thread_ids._compose`.
            #
            # It is never *empty* here whatever the dataset said: `items` always comes back
            # from `get_dataset`, and `put_dataset` mints a `uuid4().hex` for an item that
            # carries no id — the `--fixture` path included, since the CLI stores the file
            # before running it. So this handles the ids an author chose badly, not absent
            # ones; a stand-in for a missing id would be a branch nothing reaches.
            thread_id = eval_thread_id(tenant_id, str(run["id"]), item_id)
            if thread_id is None:
                # This item's error, not the run's — the same choice the rubric check below
                # makes, so one unusable id does not cost every other item its score.
                raise ValueError(f"item_id is not usable as a thread id: {item_id[:80]!r}")
            req_ctx = RequestContext(
                settings=settings,
                auth=auth,
                manifest_id=candidate_manifest,
                thread_id=thread_id,
            )
            # Inside the try: a rubric that is not a mapping used to raise here and abandon the
            # whole run, so one malformed item in a stored dataset took every other item's score
            # with it and the run reported nothing. It is this item's error now.
            raw_rubric = item.get("rubric") or item.get("rubric_json") or {}
            if not isinstance(raw_rubric, dict):
                # Named, because this row is what the dataset author reads. `dict()` on a
                # string raises "dictionary update sequence element #0 has length 1", which
                # restates the exception and never mentions which field was wrong.
                raise TypeError(f"rubric must be a mapping, got {type(raw_rubric).__name__}")
            rubric = dict(raw_rubric)
            if use_llm_judge and "llm_judge" not in rubric:
                rubric = {**rubric, "llm_judge": True}
            if mock:
                answer = _mock_answer(rubric)
                trajectory = _mock_trajectory(rubric)
            else:
                assert resolved is not None
                async with async_run_with_context(req_ctx):
                    # Dataset items are written with `eval:write`; the compiled agent
                    # screens the turn, and a refusal becomes this item's error score.
                    messages = [ChatMessage(role="user", content=user_input)]
                    agent = await build_tenant_agent(
                        settings,
                        manifest=resolved.manifest,
                        tools=tools,
                        tenant_id=tenant_id,
                    )
                    result = await agent.invoke(InvokeInput(messages=messages, thread_id=req_ctx.thread_id))
                answer = result.final.content if result.final else ""
                trajectory = trajectory_of(list(result.messages))
            heuristic = _score_answer(answer, rubric, trajectory)
            if _wants_llm_judge(rubric, deterministic_judge=deterministic_judge) and not mock:
                judged = await _maybe_llm_judge(
                    settings,
                    user_input=user_input,
                    answer=answer,
                    rubric=rubric,
                    heuristic=heuristic,
                )
                ok = bool(judged["pass"])
                score_row = {
                    "item_id": item_id,
                    "pass": ok,
                    "score": judged["score"],
                    "rule": judged["rule"],
                    "answer": answer[:500],
                    "mock": mock,
                    "reason": judged.get("reason"),
                }
            else:
                ok, score, rule = heuristic
                score_row = {
                    "item_id": item_id,
                    "pass": ok,
                    "score": score,
                    "rule": rule,
                    "answer": answer[:500],
                    "mock": mock,
                }
            score_row["tool_calls"] = len(trajectory.tool_names)
            score_row["tool_errors"] = trajectory.errors
            if ok:
                passes += 1
            else:
                fails += 1
            scores.append(score_row)
        except Exception as exc:
            # Counted in both: `fail_count` keeps meaning "did not pass", which the CLI's exit
            # code and every existing reader rely on; `error_count` is the subset that never
            # reached the scorer, so a malformed dataset reads differently from a rejected one.
            fails += 1
            errors += 1
            scores.append({"item_id": item_id, "pass": False, "error": str(exc)})
            # `loggable`, because `item_id` is dataset content and this line is now reachable
            # deliberately -- an id chosen to be unusable takes the branch above straight here.
            # A newline in it would otherwise forge a second log record.
            logger.exception("eval_item_failed item=%s", loggable(item_id, limit=80))

    completed = await eval_store.complete_run(
        settings,
        tenant_id,
        run["id"],
        pass_count=passes,
        fail_count=fails,
        error_count=errors,
        scores=scores,
    )
    return completed or {
        **run,
        "pass_count": passes,
        "fail_count": fails,
        "error_count": errors,
        "scores": scores,
    }


def _mock_answer(rubric: dict[str, Any]) -> str:
    """Deterministic answer for CI — uses mock_answer / expect / contains."""
    if rubric.get("mock_answer") is not None:
        return str(rubric["mock_answer"])
    if rubric.get("expect") is not None:
        return str(rubric["expect"])
    if rubric.get("equals") is not None:
        return str(rubric["equals"])
    contains = rubric.get("contains")
    if contains is not None:
        return f"Felix mock reply containing {contains}"
    try:
        min_chars = int(rubric.get("min_chars") or 0)
    except TypeError, ValueError, OverflowError:
        # Unscoreable, and `_score_answer` says so as `invalid_rubric`. Raising here would make
        # the item an error instead, which the counts cannot tell from an honest rejection.
        return "ok"
    if min_chars > 0:
        return "x" * min_chars
    return "ok"


__all__ = ["Trajectory", "start_run", "trajectory_of"]
