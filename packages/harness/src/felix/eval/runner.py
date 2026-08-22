"""Offline eval runner — score dataset items against a candidate manifest."""

from __future__ import annotations

import logging
from typing import Any

from felix.config import Settings
from felix.context import AuthContext, RequestContext, async_run_with_context
from felix.eval import store as eval_store
from felix.patterns.types import ChatMessage, InvokeInput
from felix.runtime import build_tenant_agent, resolve_tenant_manifest

logger = logging.getLogger("felix.eval.runner")


def _score_answer(answer: str, rubric: dict[str, Any]) -> tuple[bool, float, str]:
    """Heuristic scorer — expects / contains / min_chars."""
    expect = rubric.get("expect") or rubric.get("equals")
    if expect is not None:
        ok = answer.strip() == str(expect).strip()
        return ok, 1.0 if ok else 0.0, "equals"
    contains = rubric.get("contains")
    if contains is not None:
        ok = str(contains).lower() in answer.lower()
        return ok, 1.0 if ok else 0.0, "contains"
    min_chars = int(rubric.get("min_chars") or 0)
    if min_chars:
        ok = len(answer.strip()) >= min_chars
        return ok, 1.0 if ok else 0.0, "min_chars"
    # Default: non-empty answer passes.
    ok = bool(answer.strip())
    return ok, 1.0 if ok else 0.0, "nonempty"


async def start_run(
    settings: Settings,
    *,
    tools: Any = None,
    tenant_id: str,
    dataset_name: str,
    candidate_manifest: str,
    manifest_version: int | None = None,
    mock: bool = False,
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

    resolved = None
    if not mock:
        try:
            resolved = await resolve_tenant_manifest(settings, tenant_id, candidate_manifest)
        except Exception as exc:
            logger.exception("eval_resolve_failed")
            completed = await eval_store.complete_run(
                settings,
                tenant_id,
                run["id"],
                pass_count=0,
                fail_count=len(items),
                scores=[{"error": str(exc)}],
            )
            return completed or run

    for item in items:
        item_id = str(item.get("item_id") or item.get("id") or "")
        user_input = str(item.get("user_input") or "")
        rubric = dict(item.get("rubric") or item.get("rubric_json") or {})
        req_ctx = RequestContext(
            settings=settings,
            auth=auth,
            manifest_id=candidate_manifest,
            thread_id=f"{tenant_id}:eval:{run['id']}:{item_id}",
        )
        try:
            if mock:
                answer = _mock_answer(rubric)
            else:
                assert resolved is not None
                async with async_run_with_context(req_ctx):
                    agent = await build_tenant_agent(
                        settings,
                        manifest=resolved.manifest,
                        tools=tools,
                        tenant_id=tenant_id,
                    )
                    result = await agent.invoke(
                        InvokeInput(
                            messages=[ChatMessage(role="user", content=user_input)],
                            thread_id=req_ctx.thread_id,
                        )
                    )
                answer = result.final.content if result.final else ""
            ok, score, rule = _score_answer(answer, rubric)
            if ok:
                passes += 1
            else:
                fails += 1
            scores.append(
                {
                    "item_id": item_id,
                    "pass": ok,
                    "score": score,
                    "rule": rule,
                    "answer": answer[:500],
                    "mock": mock,
                }
            )
        except Exception as exc:
            fails += 1
            scores.append({"item_id": item_id, "pass": False, "error": str(exc)})
            logger.exception("eval_item_failed item=%s", item_id)

    completed = await eval_store.complete_run(
        settings,
        tenant_id,
        run["id"],
        pass_count=passes,
        fail_count=fails,
        scores=scores,
    )
    return completed or {**run, "pass_count": passes, "fail_count": fails, "scores": scores}


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
    min_chars = int(rubric.get("min_chars") or 0)
    if min_chars:
        return ("x" * min_chars) if min_chars else "ok"
    return "ok"


__all__ = ["start_run"]
