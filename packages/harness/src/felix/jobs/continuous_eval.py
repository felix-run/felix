"""Continuous eval — sample recent audit user turns against active canaries."""

from __future__ import annotations

import logging
from typing import Any

from felix.audit import store as audit_store
from felix.config import Settings
from felix.eval import store as eval_store
from felix.eval.runner import start_run
from felix.manifests import store as manifest_store
from felix.tools.builtins import default_tool_provider

logger = logging.getLogger("felix.jobs.continuous_eval")


async def run_continuous_eval(
    settings: Settings, *, tenant_id: str = "default"
) -> dict[str, Any]:
    """Sample recent audit user_input and score canary manifests via start_run."""
    active = await manifest_store.list_active(settings, tenant_id)
    canaries = [
        a for a in active if a.get("canary_version") and a.get("canary_weight", 0) > 0
    ]
    if not canaries:
        return {"runs": 0, "reason": "no_canaries"}

    events, _ = await audit_store.list_events(settings, tenant_id, limit=50)
    samples = [
        str((e.get("payload") or e.get("payload_json") or {}).get("user_input") or "")
        for e in events
        if (e.get("payload") or e.get("payload_json") or {}).get("user_input")
    ]
    samples = [s for s in samples if s][:5]

    await eval_store.put_dataset(
        settings,
        tenant_id,
        "continuous",
        description="Auto-sampled continuous eval",
        items=[
            {
                "item_id": f"s{i}",
                "user_input": s,
                "rubric": {"min_chars": 1},
            }
            for i, s in enumerate(samples)
        ]
        if samples
        else [{"item_id": "ping", "user_input": "ping", "rubric": {"min_chars": 1}}],
    )

    tools = default_tool_provider()
    runs = 0
    results: list[dict[str, Any]] = []
    for c in canaries:
        name = c["name"]
        version = c.get("canary_version")
        try:
            completed = await start_run(
                settings,
                tools=tools,
                tenant_id=tenant_id,
                dataset_name="continuous",
                candidate_manifest=name,
                manifest_version=int(version) if version is not None else None,
            )
            results.append(
                {
                    "manifest": name,
                    "run_id": completed.get("id"),
                    "pass_count": completed.get("pass_count"),
                    "fail_count": completed.get("fail_count"),
                }
            )
            runs += 1
        except Exception:
            logger.exception(
                "continuous_eval_failed tenant=%s manifest=%s", tenant_id, name
            )
        logger.info(
            "continuous_eval tenant=%s manifest=%s canary=%s samples=%s",
            tenant_id,
            name,
            version,
            len(samples),
        )

    return {"runs": runs, "samples": len(samples), "results": results}


__all__ = ["run_continuous_eval"]
