"""One `tool_call` step, run inside its own fiber (op `tool`, kind `workflow_tool`).

The step's tool is the one its manifest binds, governed exactly as the agent would have it: the
manifest is resolved as the run's starter, checked against the pin taken when the run started, and
compiled, and the tool is called through the governed executor that compile returns — secret
masking, policies, screening, limits, approvals and the rest all apply — and it is dispatched by the
agent loop's own `ToolRunner`, so the plugin and manifest tool hooks run and the `tool_call` audit
row is written as for a model's call. The arguments are screened as a direct MCP call's are.

Two things are checked before the call:

- the tool still exists on that manifest, and
- its live input schema still accepts what the step was wired against (`schema_drift`). The
  definition stores the schema the editor listed when the step was wired; a tool that has since
  gained a required argument, or changed one the step fills, fails here rather than at the far end.

**Running twice.** A fiber whose worker died mid-call is claimed again. Whether the effect happened
cannot be known from outside, so the call is repeated only for a tool marked `replay_safe`; any
other fails with that said. The marker is written before the call (`checkpoint`).

What a client reads of a failure is written here, never an exception's text.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from felix.config import Settings
from felix.logging_setup import loggable
from felix.workflows.ports import Port, port_mismatch

logger = logging.getLogger("felix.workflows.tool_step")

TOOL_CHILD_KIND = "workflow_tool"
# The state key a call's fiber writes before it calls, so a replay knows the call may have run.
TOOL_BEGAN = "tool_began"


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def schema_drift(stored: dict[str, Any], live: dict[str, Any], sent: set[str]) -> str | None:
    """Why the live schema no longer takes the arguments the step sends, or `None`.

    `sent` is every argument the step supplies (fixed `args` and wired inputs). A live required
    argument the step does not send, and a sent argument the live schema lost or retyped, are
    drift; a new optional argument is not.
    """
    live_props = _dict(live.get("properties"))
    stored_props = _dict(stored.get("properties"))
    raw_required = live.get("required")
    required: list[Any] = raw_required if isinstance(raw_required, list) else []
    missing = sorted(str(r) for r in required if isinstance(r, str) and r not in sent)
    if missing:
        return f"the tool now requires {', '.join(missing[:5])}"
    for name in sorted(sent):
        if live_props and name not in live_props:
            return f"the tool no longer takes {name[:64]}"
        before, now = stored_props.get(name), live_props.get(name)
        if isinstance(before, dict) and isinstance(now, dict):
            reason = port_mismatch(Port(name, before), Port(name, now))
            if reason is not None:
                return f"argument {name[:64]} changed type ({reason})"
    return None


async def run_tool_step(
    settings: Settings,
    row: dict[str, Any],
    step: dict[str, Any],
    checkpoint: Callable[[], Awaitable[bool]],
) -> dict[str, Any]:
    """Make the call; returns `{"output": ...}` or `{"error": "..."}` for the step's result.

    `checkpoint` persists the fiber's state (with `TOOL_BEGAN` set) and says whether this claim
    still owns the row; `False` means another worker does, and nothing is called.
    """
    from felix.context import RequestContext, async_run_with_context
    from felix.durability.fibers import auth_from_state
    from felix.durability.runs import RUN_NOT_AFTER_EXTRA

    tenant_id = row["tenant_id"]
    state = row["state_json"]
    stored_auth = state.get("auth") if isinstance(state.get("auth"), dict) else {}
    manifest_id, thread = str(step.get("manifest_id") or ""), str(step.get("thread_id") or "")
    context = RequestContext(
        settings=settings,
        auth=auth_from_state(tenant_id, stored_auth),
        manifest_id=manifest_id,
        thread_id=thread,
    )
    if isinstance(state.get("expires_at"), int):
        context.extras[RUN_NOT_AFTER_EXTRA] = state["expires_at"]
    from felix.manifests.inbound_auth import InboundAuthError
    from felix.manifests.pin import ManifestDriftError

    try:
        async with async_run_with_context(context):
            return await _call(settings, row, step, context.auth, checkpoint)
    except ManifestDriftError:
        return {"error": f"manifest {manifest_id[:200]} changed since the run started"}
    except InboundAuthError:
        return {"error": f"the run's starter may not use manifest {manifest_id[:200]}"}
    except Exception:
        logger.exception(
            "workflow tool step fiber=%s tool=%s failed", row.get("id"), loggable(step.get("tool"))
        )
        return {"error": "the tool call failed"}


async def _governed_tools(
    settings: Settings, row: dict[str, Any], manifest_id: str, thread: str, auth: Any
) -> tuple[Any, list[Any]]:
    """The step's manifest, resolved as the starter and held to its pin, and its governed tools."""
    from felix.manifests.pin import assert_resume_pin
    from felix.runtime import build_tenant_agent, prepare_tenant_invoke, resolve_tenant_manifest
    from felix.tools.builtins import default_tool_provider

    tenant_id, state = row["tenant_id"], row["state_json"]
    resolved = await resolve_tenant_manifest(settings, tenant_id, manifest_id, thread_id=thread)
    pinned = state.get("pin") if isinstance(state.get("pin"), dict) else None
    if pinned:
        # Forced, as the invoke op forces it: authority carried in state runs only the code it
        # was checked against.
        await assert_resume_pin(
            settings,
            tenant_id,
            {**pinned, "pin_compile": True},
            resolved.manifest,
            version=resolved.version,
            resolved_out=resolved.sub_agents,
        )
    await prepare_tenant_invoke(settings, resolved=resolved, auth=auth, thread_id=thread)
    agent = await build_tenant_agent(
        settings,
        manifest=resolved.manifest,
        sub_agents=resolved.sub_agents,
        tools=default_tool_provider(),
        tenant_id=tenant_id,
        skill_owner=auth.skill_owner,
    )
    return resolved.manifest, list(getattr(agent, "tools", None) or [])


async def _call(
    settings: Settings,
    row: dict[str, Any],
    step: dict[str, Any],
    auth: Any,
    checkpoint: Callable[[], Awaitable[bool]],
) -> dict[str, Any]:
    from felix.governance.screening import InboundScreeningError
    from felix.governance.tool_screening import screen_tool_arguments
    from felix.manifest_hooks import ManifestHooks
    from felix.patterns.tool_runner import ToolRunner
    from felix.patterns.types import ToolCall
    from felix.tools.types import tool_input_schema

    state = row["state_json"]
    manifest_id, thread = str(step.get("manifest_id") or ""), str(step.get("thread_id") or "")
    manifest, tools = await _governed_tools(settings, row, manifest_id, thread, auth)
    name = str(step.get("tool") or "")
    tool = next((t for t in tools if t.name == name), None)
    if tool is None:
        return {"error": f"manifest {manifest_id[:200]} does not bind tool {name[:200]}"}
    args: dict[str, Any] = dict(step.get("args") or {})
    drift = schema_drift(dict(step.get("input_schema") or {}), tool_input_schema(tool), set(args))
    if drift is not None:
        return {"error": f"{drift}; rewire the step against the tool as it is now"}
    if state.get(TOOL_BEGAN) and not getattr(tool, "replay_safe", False):
        return {"error": "the call may have run before its worker stopped; it is not repeated"}
    try:
        args = await screen_tool_arguments(manifest, args, settings)
    except InboundScreeningError:
        return {"error": "the arguments were refused by screening"}
    state[TOOL_BEGAN] = True
    if not await checkpoint():
        return {"lost": True}
    # Through the agent loop's own dispatch, so the call gets everything a model's would beyond
    # the governed executor: the plugin `before_tool` / `after_tool` hooks, the manifest's
    # `pre_tool_use` / `post_tool_use`, the `tool_call` audit row, counters and the span.
    runner = ToolRunner(
        tool_map={tool.name: tool},
        manifest_id=manifest_id,
        manifest_hooks=ManifestHooks(list(manifest.spec.hooks), manifest.metadata.name)
        if manifest.spec.hooks
        else None,
    )
    call = ToolCall(id=f"workflow-{step.get('step_id') or 'step'}", name=name, args=args)
    _fatal, message, _terminate, denied = await runner.dispatch(call, thread)
    return _outcome(str(message.content or ""), denied)


_FAILURE_TAG = re.compile(r"^\[(tool error|error|fatal)/([a-z_]+)\]")


def _outcome(text: str, denied: bool) -> dict[str, Any]:
    """The step's result from the tool message dispatch produced. Failures are named by their
    code only: the message itself can carry an exception's text."""
    from felix.tools.types import is_failure_content

    if denied:
        return {"error": "governance or a hook refused the call"}
    if is_failure_content(text):
        tag = _FAILURE_TAG.match(text)
        if tag is None:
            return {"error": "the tool call failed"}
        kind, code = tag.groups()
        return {
            "error": f"the tool reported {code}" if kind == "tool error" else f"the tool call failed ({code})"
        }
    try:
        return {"output": json.loads(text)}
    except ValueError:
        return {"output": text}


__all__ = ["TOOL_BEGAN", "TOOL_CHILD_KIND", "run_tool_step", "schema_drift"]
