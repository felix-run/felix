"""Plan tools for the `deep` pattern — create, update, and read a persisted plan.

Split out of `patterns/__init__.py`, which had grown to hold six pattern builders, the
composite agent, and these tools. A package `__init__` should wire a package together,
not implement it.
"""

from __future__ import annotations

from typing import Any

from felix.tools.types import Tool, define_tool

# A conflict means someone else wrote the plan between our read and write; a few
# re-reads settle any realistic contention without looping on a hot row forever.
_UPDATE_ATTEMPTS = 3


# The keys a model reaches for when it names a step. `steps` was declared as a bare
# array, so models guessed: one sent `description` for every step, and the plan was
# stored with three empty titles (measured on a local deployment, 2026-10-03). The
# schema now says `title`; the handler still accepts the other spellings, because a
# step whose words are dropped is worse than one spelled unexpectedly.
_STEP_TITLE_KEYS = ("title", "text", "description", "name")


def _step_title(step: dict[str, Any]) -> str:
    for key in _STEP_TITLE_KEYS:
        value = step.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


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
                            "title": _step_title(s),
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
            # The conversation it was written in, so `GET /plans?thread_id=` and a bare
            # `plan_get` can answer for this thread rather than for the whole tenant.
            thread_id=req.thread_id or "",
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
        # Conditional, and retried against what is actually stored: an operator may
        # have replaced the plan through `PUT /plans/{id}` since it was read, and an
        # unconditional write here would erase their edit to record a step status.
        row = await plans_store.get_plan(req.settings, req.auth.tenant_id, plan_id)
        for _ in range(_UPDATE_ATTEMPTS):
            if row is None:
                return f"error: plan not found: {plan_id}"
            plan = dict(row["plan"] or {})
            steps = [dict(step) for step in plan.get("steps") or []]
            step = next((s for s in steps if str(s.get("id")) == step_id), None)
            if step is None:
                return f"error: step not found: {step_id}"
            step["status"] = str(args.get("status") or "done")
            if args.get("note"):
                step["note"] = str(args["note"])
            plan["steps"] = steps
            try:
                updated = await plans_store.put_plan(
                    req.settings,
                    req.auth.tenant_id,
                    plan_id,
                    plan=plan,
                    # Backfills a plan stored without one; otherwise it is what is stored.
                    manifest_id=row.get("manifest_id") or req.manifest_id or "",
                    # Same backfill, for a plan written before plans named their thread.
                    thread_id=row.get("thread_id") or req.thread_id or "",
                    expected_updated_at=row["updated_at"],
                )
            except plans_store.PlanConflict as conflict:
                row = conflict.current
                continue
            return json.dumps({"id": updated["id"], "plan": updated["plan"]}, separators=(",", ":"))
        return f"error: plan {plan_id} kept changing while updating step {step_id}; try again"

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
        # The newest plan on *this* conversation. It was the tenant's newest, so an agent
        # in one thread could read, and go on to update, a plan another thread was
        # following. Outside a chat there is no thread to scope to, and it stays tenant-wide.
        items = await plans_store.list_plans(
            req.settings, req.auth.tenant_id, limit=1, thread_id=req.thread_id or None
        )
        if not items:
            return "error: no plans on this thread" if req.thread_id else "error: no plans for tenant"
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
                    "steps": {
                        "type": "array",
                        "description": "The steps in order: each a string, or an object with a title.",
                        "items": {
                            "anyOf": [
                                {"type": "string"},
                                {
                                    "type": "object",
                                    "properties": {
                                        "id": {"type": "string"},
                                        "title": {"type": "string"},
                                        "status": {"type": "string"},
                                    },
                                    "required": ["title"],
                                },
                            ]
                        },
                    },
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
            description="Fetch a plan by id, or the most recently updated plan in this conversation.",
            args_schema={
                "type": "object",
                "properties": {"plan_id": {"type": "string"}},
            },
            handler=plan_get,
        ),
    ]
