"""Offline eval datasets and runs."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from felix.auth.mgmt import (
    SCOPE_EVAL_READ,
    SCOPE_EVAL_WRITE,
    require_mgmt_scopes,
    tenant_id_from_request,
)
from pydantic import BaseModel, Field

router = APIRouter(tags=["Eval"])


class DatasetUpsert(BaseModel):
    model_config = {"extra": "forbid"}

    description: str = ""
    items: list[dict[str, Any]] = Field(default_factory=list)


class EvalRunRequest(BaseModel):
    model_config = {"extra": "forbid"}

    dataset_name: str | None = None
    candidate_manifest: str
    manifest_version: int | None = None
    # When true, skip LLM judges and use rubric heuristics only.
    deterministic_judge: bool = False
    # When true (and not deterministic), score items with an LLM judge.
    use_llm_judge: bool = False


@router.get("/datasets")
async def list_datasets(request: Request) -> dict[str, Any]:
    from felix.eval import store as eval_store

    require_mgmt_scopes(request, SCOPE_EVAL_READ)
    items = await eval_store.list_datasets(request.app.state.settings, tenant_id_from_request(request))
    return {"items": items, "datasets": items}


@router.put("/datasets/{name}")
async def upsert_dataset(name: str, body: DatasetUpsert, request: Request) -> Any:
    from felix.eval import store as eval_store

    require_mgmt_scopes(request, SCOPE_EVAL_WRITE)
    return await eval_store.put_dataset(
        request.app.state.settings,
        tenant_id_from_request(request),
        name,
        description=body.description,
        items=body.items,
    )


@router.get("/datasets/{name}")
async def get_dataset(name: str, request: Request) -> Any:
    from felix.eval import store as eval_store

    require_mgmt_scopes(request, SCOPE_EVAL_READ)
    row = await eval_store.get_dataset(request.app.state.settings, tenant_id_from_request(request), name)
    if row is None:
        raise HTTPException(status_code=404, detail="not_found")
    return row


@router.post("/runs/compare")
async def compare_eval_runs(body: dict[str, Any], request: Request) -> Any:
    """Comparative eval: baseline vs candidates on one dataset."""
    from felix.eval.compare import EvalHarness, run_comparative

    require_mgmt_scopes(request, SCOPE_EVAL_WRITE)
    dataset = body.get("dataset_name") or body.get("dataset")
    if not dataset:
        raise HTTPException(status_code=400, detail="dataset_name_required")
    baseline_raw = body.get("baseline") or {}
    if not baseline_raw.get("manifest"):
        raise HTTPException(status_code=400, detail="baseline_manifest_required")
    baseline = EvalHarness(
        name=str(baseline_raw.get("name") or "baseline"),
        manifest=str(baseline_raw["manifest"]),
        mock=bool(body.get("mock") or baseline_raw.get("mock")),
    )
    candidates = [
        EvalHarness(
            name=str(c.get("name") or f"candidate-{i}"),
            manifest=str(c["manifest"]),
            mock=bool(body.get("mock") or c.get("mock")),
        )
        for i, c in enumerate(body.get("candidates") or [])
        if c.get("manifest")
    ]
    threshold = body.get("judge_threshold")
    return await run_comparative(
        request.app.state.settings,
        tools=request.app.state.tools,
        tenant_id=tenant_id_from_request(request),
        dataset_name=str(dataset),
        baseline=baseline,
        candidates=candidates,
        judge_threshold=float(threshold) if threshold is not None else None,
        mock=bool(body.get("mock")),
    )


@router.post("/runs")
async def start_eval_run(body: EvalRunRequest, request: Request) -> Any:
    from felix.eval.runner import start_run

    require_mgmt_scopes(request, SCOPE_EVAL_WRITE)
    if not body.dataset_name:
        raise HTTPException(status_code=400, detail="dataset_name_required")
    return await start_run(
        request.app.state.settings,
        tools=request.app.state.tools,
        tenant_id=tenant_id_from_request(request),
        dataset_name=body.dataset_name,
        candidate_manifest=body.candidate_manifest,
        manifest_version=body.manifest_version,
        deterministic_judge=body.deterministic_judge,
        use_llm_judge=not body.deterministic_judge and bool(body.use_llm_judge),
    )


@router.post("/datasets/{name}/run")
async def run_dataset(name: str, body: EvalRunRequest, request: Request) -> Any:
    """Alias for chat-ui: POST /eval/datasets/{name}/run."""
    from felix.eval.runner import start_run

    require_mgmt_scopes(request, SCOPE_EVAL_WRITE)
    return await start_run(
        request.app.state.settings,
        tools=request.app.state.tools,
        tenant_id=tenant_id_from_request(request),
        dataset_name=name,
        candidate_manifest=body.candidate_manifest,
        manifest_version=body.manifest_version,
        mock=False,
        deterministic_judge=body.deterministic_judge,
        use_llm_judge=not body.deterministic_judge and bool(body.use_llm_judge),
    )


@router.get("/runs")
async def list_eval_runs(request: Request) -> dict[str, Any]:
    from felix.eval import store as eval_store

    require_mgmt_scopes(request, SCOPE_EVAL_READ)
    items = await eval_store.list_runs(request.app.state.settings, tenant_id_from_request(request))
    return {"items": items, "runs": items}


@router.get("/runs/{run_id}")
async def get_eval_run(run_id: str, request: Request) -> Any:
    from felix.eval import store as eval_store

    require_mgmt_scopes(request, SCOPE_EVAL_READ)
    row = await eval_store.get_run(request.app.state.settings, tenant_id_from_request(request), run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="not_found")
    return row
