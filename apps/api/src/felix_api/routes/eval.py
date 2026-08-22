"""Offline eval datasets and runs."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from felix.context import try_get_context
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
    # Accepted for chat-ui compatibility; Python eval uses heuristic rubrics.
    deterministic_judge: bool = False


def _tenant(request: Request) -> str:
    ctx = try_get_context()
    if ctx is not None:
        return ctx.auth.tenant_id
    auth = getattr(request.state, "auth", None)
    return getattr(auth, "tenant_id", "default") if auth else "default"


@router.get("/datasets")
async def list_datasets(request: Request) -> dict[str, Any]:
    from felix.eval import store as eval_store

    items = await eval_store.list_datasets(request.app.state.settings, _tenant(request))
    return {"items": items, "datasets": items}


@router.put("/datasets/{name}")
async def upsert_dataset(name: str, body: DatasetUpsert, request: Request) -> Any:
    from felix.eval import store as eval_store

    return await eval_store.put_dataset(
        request.app.state.settings,
        _tenant(request),
        name,
        description=body.description,
        items=body.items,
    )


@router.get("/datasets/{name}")
async def get_dataset(name: str, request: Request) -> Any:
    from felix.eval import store as eval_store

    row = await eval_store.get_dataset(request.app.state.settings, _tenant(request), name)
    if row is None:
        raise HTTPException(status_code=404, detail="not_found")
    return row


@router.post("/runs/compare")
async def compare_eval_runs(body: dict[str, Any], request: Request) -> Any:
    """Comparative eval: baseline vs candidates on one dataset."""
    from felix.eval.compare import EvalHarness, run_comparative

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
        tenant_id=_tenant(request),
        dataset_name=str(dataset),
        baseline=baseline,
        candidates=candidates,
        judge_threshold=float(threshold) if threshold is not None else None,
        mock=bool(body.get("mock")),
    )


@router.post("/runs")
async def start_eval_run(body: EvalRunRequest, request: Request) -> Any:
    from felix.eval.runner import start_run

    if not body.dataset_name:
        raise HTTPException(status_code=400, detail="dataset_name_required")
    return await start_run(
        request.app.state.settings,
        tools=request.app.state.tools,
        tenant_id=_tenant(request),
        dataset_name=body.dataset_name,
        candidate_manifest=body.candidate_manifest,
        manifest_version=body.manifest_version,
    )


@router.post("/datasets/{name}/run")
async def run_dataset(name: str, body: EvalRunRequest, request: Request) -> Any:
    """Alias for chat-ui: POST /eval/datasets/{name}/run."""
    from felix.eval.runner import start_run

    _ = body.deterministic_judge
    return await start_run(
        request.app.state.settings,
        tools=request.app.state.tools,
        tenant_id=_tenant(request),
        dataset_name=name,
        candidate_manifest=body.candidate_manifest,
        manifest_version=body.manifest_version,
        mock=False,
    )


@router.get("/runs")
async def list_eval_runs(request: Request) -> dict[str, Any]:
    from felix.eval import store as eval_store

    items = await eval_store.list_runs(request.app.state.settings, _tenant(request))
    return {"items": items, "runs": items}


@router.get("/runs/{run_id}")
async def get_eval_run(run_id: str, request: Request) -> Any:
    from felix.eval import store as eval_store

    row = await eval_store.get_run(request.app.state.settings, _tenant(request), run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="not_found")
    return row
