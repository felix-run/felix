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

    dataset_name: str
    candidate_manifest: str
    manifest_version: int | None = None


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
    return {"items": items}


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


@router.post("/runs")
async def start_eval_run(body: EvalRunRequest, request: Request) -> Any:
    from felix.eval.runner import start_run

    return await start_run(
        request.app.state.settings,
        tools=request.app.state.tools,
        tenant_id=_tenant(request),
        dataset_name=body.dataset_name,
        candidate_manifest=body.candidate_manifest,
        manifest_version=body.manifest_version,
    )


@router.get("/runs")
async def list_eval_runs(request: Request) -> dict[str, Any]:
    from felix.eval import store as eval_store

    items = await eval_store.list_runs(request.app.state.settings, _tenant(request))
    return {"items": items}


@router.get("/runs/{run_id}")
async def get_eval_run(run_id: str, request: Request) -> Any:
    from felix.eval import store as eval_store

    row = await eval_store.get_run(request.app.state.settings, _tenant(request), run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="not_found")
    return row
