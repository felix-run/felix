"""Plan tools for the `deep` pattern — create, update, and read a persisted plan.

Split out of `patterns/__init__.py`, which had grown to hold six pattern builders, the
composite agent, and these tools. A package `__init__` should wire a package together,
not implement it.
"""

from __future__ import annotations

from typing import Any

from felix.tools.types import Tool, define_tool


def _plan_tools() -> list[Tool]:
    async def plan_create(args: dict[str, Any], _ctx: Any = None) -> str:
        import json
        import uuid

        from felix.context import try_get_context
        from felix.plans import store as plans_store

        req = try_get_context()
        if req is None:
            return "error: no request context for plan_create"
        plan_id = str(args.get("plan_id") or uuid.uuid4().hex[:12])
        title = str(args.get("title") or "")
        goal = str(args.get("goal") or "")
        raw_steps = args.get("steps")
        steps: list[dict[str, Any]]
        if isinstance(raw_steps, list):
            steps = []
            for i, s in enumerate(raw_steps):
                if isinstance(s, dict):
                    steps.append(
                        {
                            "id": str(s.get("id") or i + 1),
                            "title": str(s.get("title") or s.get("text") or ""),
                            "status": str(s.get("status") or "pending"),
                        }
                    )
                else:
                    steps.append({"id": str(i + 1), "title": str(s), "status": "pending"})
        elif goal:
            steps = [{"id": "1", "title": goal, "status": "pending"}]
        else:
            steps = []
        body = {
            "title": title or goal or "untitled",
            "goal": goal,
            "steps": steps,
            "status": "active",
        }
        row = await plans_store.put_plan(
            req.settings,
            req.auth.tenant_id,
            plan_id,
            plan=body,
            manifest_id=req.manifest_id or "",
        )
        return json.dumps({"id": row["id"], "plan": row["plan"]}, separators=(",", ":"))

    async def plan_update_step(args: dict[str, Any], _ctx: Any = None) -> str:
        import json

        from felix.context import try_get_context
        from felix.plans import store as plans_store

        req = try_get_context()
        if req is None:
            return "error: no request context for plan_update_step"
        plan_id = str(args.get("plan_id") or "")
        step_id = str(args.get("step_id") or "")
        if not plan_id or not step_id:
            return "error: plan_id and step_id required"
        row = await plans_store.get_plan(req.settings, req.auth.tenant_id, plan_id)
        if row is None:
            return f"error: plan not found: {plan_id}"
        plan = dict(row["plan"] or {})
        steps = list(plan.get("steps") or [])
        found = False
        for step in steps:
            if str(step.get("id")) == step_id:
                step["status"] = str(args.get("status") or "done")
                if args.get("note"):
                    step["note"] = str(args["note"])
                found = True
                break
        if not found:
            return f"error: step not found: {step_id}"
        plan["steps"] = steps
        updated = await plans_store.put_plan(
            req.settings,
            req.auth.tenant_id,
            plan_id,
            plan=plan,
            manifest_id=row.get("manifest_id") or req.manifest_id or "",
            expires_at=row.get("expires_at"),
        )
        return json.dumps({"id": updated["id"], "plan": updated["plan"]}, separators=(",", ":"))

    async def plan_get(args: dict[str, Any], _ctx: Any = None) -> str:
        import json

        from felix.context import try_get_context
        from felix.plans import store as plans_store

        req = try_get_context()
        if req is None:
            return "error: no request context for plan_get"
        plan_id = str(args.get("plan_id") or "")
        if plan_id:
            row = await plans_store.get_plan(req.settings, req.auth.tenant_id, plan_id)
            if row is None:
                return f"error: plan not found: {plan_id}"
            return json.dumps({"id": row["id"], "plan": row["plan"]}, separators=(",", ":"))
        items = await plans_store.list_plans(req.settings, req.auth.tenant_id, limit=1)
        if not items:
            return "error: no plans for tenant"
        row = items[0]
        return json.dumps({"id": row["id"], "plan": row["plan"]}, separators=(",", ":"))

    return [
        define_tool(
            name="plan_create",
            description="Create a multi-step plan for a complex task.",
            args_schema={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "goal": {"type": "string"},
                    "plan_id": {"type": "string"},
                    "steps": {"type": "array"},
                },
            },
            handler=plan_create,
        ),
        define_tool(
            name="plan_update_step",
            description="Update a plan step status.",
            args_schema={
                "type": "object",
                "properties": {
                    "plan_id": {"type": "string"},
                    "step_id": {"type": "string"},
                    "status": {"type": "string"},
                    "note": {"type": "string"},
                },
            },
            handler=plan_update_step,
        ),
        define_tool(
            name="plan_get",
            description="Fetch a plan by id, or the most recently updated plan.",
            args_schema={
                "type": "object",
                "properties": {"plan_id": {"type": "string"}},
            },
            handler=plan_get,
        ),
    ]
