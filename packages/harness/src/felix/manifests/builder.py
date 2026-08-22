"""Compile a Manifest into a runnable Agent with governance wrappers."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from felix.auth.context import AuthContext
from felix.context import try_get_context
from felix.manifests.loader import load_bundled, parse_manifest
from felix.manifests.schema import (
    ApprovalRule,
    Manifest,
    Policy,
    any_limit,
    guardrails_enabled,
    judges_enabled,
)
from felix.observability.metrics import record_counter
from felix.observability.tracing import manifest_span
from felix.patterns.registry import get_pattern, list_patterns
from felix.patterns.types import Agent
from felix.tools.executor import wrap_executor
from felix.tools.provider import ToolProvider
from felix.tools.types import (
    Tool,
    ToolInput,
    ToolInvocationCtx,
    ToolOutput,
    deny_output,
    is_wrapper_deny,
    tool_output_content,
)

logger = logging.getLogger("felix.manifests.builder")

# Side-effect: register built-in patterns.
import felix.patterns  # noqa: E402, F401


@dataclass
class BuildDeps:
    tools: ToolProvider
    auth: AuthContext | None = None
    soul_loader: Callable[[str], Awaitable[str] | str] | None = None
    extra_tools: list[Tool] = field(default_factory=list)
    sub_agent_builder: Callable[[str], Awaitable[Agent]] | None = None
    settings: Any | None = None
    session_store: Any | None = None
    session_strategy: Any | None = None
    object_store: Any | None = None
    tenant_id: str | None = None
    workspace_root: str | None = None
    load_agents_md: bool = False


# ---------------------------------------------------------------------------
# Governance wrappers (innermost → outermost on the call path)
# ---------------------------------------------------------------------------


def _wrap_tools(
    tools: list[Tool],
    wrapper: Callable[[Tool], Tool],
) -> list[Tool]:
    return [wrapper(t) for t in tools]


def apply_secret_masking(tools: list[Tool], secrets: list[str], manifest_id: str) -> list[Tool]:
    """Innermost: redact known secrets from tool output before anything else sees them."""
    if not secrets:
        return tools

    def wrap_one(tool: Tool) -> Tool:
        inner = tool.executor

        async def execute(
            args: ToolInput, ctx: ToolInvocationCtx | None = None
        ) -> ToolOutput:
            out = await inner.execute(args, ctx)
            if is_wrapper_deny(out):
                return out
            content = tool_output_content(out)
            for s in secrets:
                if s and s in content:
                    content = content.replace(s, "[REDACTED]")
                    record_counter(
                        "felix_secret_masking",
                        {"manifest_id": manifest_id, "tool": tool.name},
                    )
            return _replace_content(out, content)

        return _clone_tool(tool, wrap_executor(inner, execute))

    return _wrap_tools(tools, wrap_one)


def _replace_content(out: ToolOutput, content: str) -> ToolOutput:
    if isinstance(out, str):
        return content
    if isinstance(out, dict):
        return {**out, "content": content}
    out.content = content  # type: ignore[union-attr]
    return out


def _clone_tool(tool: Tool, executor: Any) -> Tool:
    return Tool(
        name=tool.name,
        description=tool.description,
        args_schema=tool.args_schema,
        executor=executor,
        raw_input_schema=tool.raw_input_schema,
        is_peer=tool.is_peer,
        peer=tool.peer,
        source=tool.source,
        fatal=tool.fatal,
    )


def apply_policies(tools: list[Tool], policies: list[Policy], manifest_id: str) -> list[Tool]:
    by_tool: dict[str, list[Policy]] = {}
    for p in policies:
        for name in p.tools:
            by_tool.setdefault(name, []).append(p)

    def wrap_one(tool: Tool) -> Tool:
        rules = by_tool.get(tool.name)
        if not rules:
            return tool
        inner = tool.executor

        async def execute(
            args: ToolInput, ctx: ToolInvocationCtx | None = None
        ) -> ToolOutput:
            req_ctx = try_get_context()
            scopes = req_ctx.auth.scopes if req_ctx else frozenset()
            for rule in rules:
                missing = [s for s in rule.required_scopes if s not in scopes]
                if missing:
                    record_counter(
                        "felix_policy_deny",
                        {"manifest_id": manifest_id, "tool": tool.name, "policy": rule.id},
                    )
                    return deny_output(
                        f"[policy denied] missing scopes for {tool.name}: {', '.join(missing)}",
                        "policy",
                    )
            return await inner.execute(args, ctx)

        return Tool(
            name=tool.name,
            description=tool.description,
            args_schema=tool.args_schema,
            executor=wrap_executor(inner, execute),
            raw_input_schema=tool.raw_input_schema,
            is_peer=tool.is_peer,
            source=tool.source,
            fatal=tool.fatal,
        )

    return _wrap_tools(tools, wrap_one)


def apply_command_screening(tools: list[Tool], screening: Any, manifest_id: str) -> list[Tool]:
    if not getattr(screening, "enabled", False):
        return tools
    import re

    compiled = [
        (re.compile(r.pattern, re.I), r.decision, r.reason or r.pattern)
        for r in getattr(screening, "rules", [])
    ]
    targets = set(getattr(screening, "target_tools", []) or [])

    def wrap_one(tool: Tool) -> Tool:
        if targets and tool.name not in targets:
            if tool.executor.transport not in {"sandbox", "container"}:
                return tool
        if not compiled and tool.executor.transport not in {"sandbox", "container"}:
            return tool
        inner = tool.executor

        async def execute(
            args: ToolInput, ctx: ToolInvocationCtx | None = None
        ) -> ToolOutput:
            cmd = str(args.get("command") or args.get("cmd") or "")
            if cmd and compiled:
                for rx, decision, reason in compiled:
                    if rx.search(cmd):
                        if decision == "deny":
                            return deny_output(
                                f"[command denied] {reason}",
                                "command",  # type: ignore[arg-type]
                            )
                        if decision == "require_approval":
                            return deny_output(
                                f"[command requires approval] {reason}",
                                "approvals",
                            )
                        break
            return await inner.execute(args, ctx)

        return Tool(
            name=tool.name,
            description=tool.description,
            args_schema=tool.args_schema,
            executor=wrap_executor(inner, execute),
            raw_input_schema=tool.raw_input_schema,
            is_peer=tool.is_peer,
            source=tool.source,
            fatal=tool.fatal,
        )

    return _wrap_tools(tools, wrap_one)


_INJECTION_MARKERS = (
    "ignore previous instructions",
    "ignore your previous instructions",
    "disregard your instructions",
    "system prompt",
)


def apply_content_screening(tools: list[Tool], screening: Any, manifest_id: str) -> list[Tool]:
    if not getattr(screening, "enabled", False):
        return tools
    on_flag = getattr(screening, "on_flag", "quarantine")
    untrusted = {"mcp", "a2a", "browser", "container", "sandbox"}
    named = set(getattr(screening, "tools", []) or [])

    def wrap_one(tool: Tool) -> Tool:
        if named and tool.name not in named:
            return tool
        if not named and tool.executor.transport not in untrusted:
            return tool
        inner = tool.executor

        async def execute(
            args: ToolInput, ctx: ToolInvocationCtx | None = None
        ) -> ToolOutput:
            out = await inner.execute(args, ctx)
            if is_wrapper_deny(out):
                return out
            content = tool_output_content(out).lower()
            if any(m in content for m in _INJECTION_MARKERS):
                record_counter(
                    "felix_content_screening",
                    {"manifest_id": manifest_id, "tool": tool.name, "action": on_flag},
                )
                if on_flag == "block":
                    return deny_output("[screening blocked] untrusted content", "screening")  # type: ignore[arg-type]
                notice = "[quarantined] tool output flagged as potentially hostile"
                if isinstance(out, str):
                    return notice
                out.content = notice
                return out
            return out

        return Tool(
            name=tool.name,
            description=tool.description,
            args_schema=tool.args_schema,
            executor=wrap_executor(inner, execute),
            raw_input_schema=tool.raw_input_schema,
            is_peer=tool.is_peer,
            source=tool.source,
            fatal=tool.fatal,
        )

    return _wrap_tools(tools, wrap_one)


def apply_limits(tools: list[Tool], limits: Any, manifest_id: str) -> list[Tool]:
    def wrap_one(tool: Tool) -> Tool:
        inner = tool.executor

        async def execute(
            args: ToolInput, ctx: ToolInvocationCtx | None = None
        ) -> ToolOutput:
            req = try_get_context()
            if req is not None:
                ls = req.limit_state
                if ls.aborted:
                    return deny_output("[limits] run aborted", "limits")
                max_calls = getattr(limits, "max_tool_calls", None)
                if max_calls is not None and ls.tool_calls >= max_calls:
                    return deny_output(
                        f"[limits] max_tool_calls ({max_calls}) exceeded",
                        "limits",
                    )
                max_hops = getattr(limits, "max_peer_hops", None)
                if (
                    max_hops is not None
                    and (tool.is_peer or tool.name.startswith("peer_"))
                    and ls.peer_hops >= max_hops
                ):
                    return deny_output(
                        f"[limits] max_peer_hops ({max_hops}) exceeded",
                        "limits",
                    )
                ls.tool_calls += 1
                if tool.is_peer or tool.name.startswith("peer_"):
                    ls.peer_hops += 1
            return await inner.execute(args, ctx)

        return Tool(
            name=tool.name,
            description=tool.description,
            args_schema=tool.args_schema,
            executor=wrap_executor(inner, execute),
            raw_input_schema=tool.raw_input_schema,
            is_peer=tool.is_peer,
            source=tool.source,
            fatal=tool.fatal,
        )

    return _wrap_tools(tools, wrap_one)


_PII_PATTERNS = (
    (__import__("re").compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "email"),
    (__import__("re").compile(r"\b\d{3}-\d{2}-\d{4}\b"), "ssn"),
)


def apply_guardrails(tools: list[Tool], guardrails: Any, manifest_id: str) -> list[Tool]:
    providers = set(getattr(guardrails, "providers", []) or [])
    if "pii" not in providers:
        return tools
    block = bool(getattr(guardrails, "block_on_match", False))

    def wrap_one(tool: Tool) -> Tool:
        inner = tool.executor

        async def execute(
            args: ToolInput, ctx: ToolInvocationCtx | None = None
        ) -> ToolOutput:
            out = await inner.execute(args, ctx)
            if is_wrapper_deny(out):
                return out
            content = tool_output_content(out)
            matched = False
            for rx, kind in _PII_PATTERNS:
                if rx.search(content):
                    matched = True
                    content = rx.sub(f"[REDACTED:{kind}]", content)
            if matched and block:
                return deny_output("[guardrails] PII blocked", "guardrails")
            if isinstance(out, str):
                return content
            out.content = content
            return out

        return Tool(
            name=tool.name,
            description=tool.description,
            args_schema=tool.args_schema,
            executor=wrap_executor(inner, execute),
            raw_input_schema=tool.raw_input_schema,
            is_peer=tool.is_peer,
            source=tool.source,
            fatal=tool.fatal,
        )

    return _wrap_tools(tools, wrap_one)


def _heuristic_judge_score(content: str, criteria: str) -> float:
    """Score tool/final output against judge criteria (0..1).

    Supports:
    * nonempty / not empty
    * min_length:N / min_chars:N
    * keyword overlap with criteria tokens (default)
    """
    text = (content or "").strip()
    c = (criteria or "").strip().lower()
    if not c:
        return 1.0 if len(text) >= 3 else 0.0
    if "nonempty" in c or "not empty" in c or c in {"relevance", "useful"}:
        return 1.0 if len(text) >= 3 else 0.0
    for prefix in ("min_length:", "min_chars:"):
        if c.startswith(prefix):
            try:
                n = max(int(c.split(":", 1)[1].strip()), 1)
            except ValueError:
                n = 1
            return min(1.0, len(text) / n)
    tokens = [t for t in c.replace(",", " ").split() if len(t) > 2]
    if not tokens:
        return 1.0 if len(text) >= 3 else 0.0
    lower = text.lower()
    words = set(lower.split())
    hit = sum(1 for t in tokens if t in words or t in lower)
    return hit / len(tokens)


def apply_judges(tools: list[Tool], guardrails: Any, manifest_id: str) -> list[Tool]:
    """Apply tool-output judges using criteria/threshold heuristics."""
    _ = manifest_id
    judges = [
        j for j in getattr(guardrails, "judges", []) if not getattr(j, "final_response", False)
    ]
    if not judges:
        return tools

    def wrap_one(tool: Tool) -> Tool:
        applicable = [
            j for j in judges if not j.target_tools or tool.name in j.target_tools
        ]
        if not applicable:
            return tool
        inner = tool.executor

        async def execute(
            args: ToolInput, ctx: ToolInvocationCtx | None = None
        ) -> ToolOutput:
            out = await inner.execute(args, ctx)
            if is_wrapper_deny(out):
                return out
            content = tool_output_content(out)
            for j in applicable:
                score = _heuristic_judge_score(content, getattr(j, "criteria", "") or "")
                threshold = float(getattr(j, "threshold", 0.7) or 0.7)
                if score < threshold:
                    return deny_output(
                        f"[judge denied] {j.name}: score={score:.2f} < {threshold}",
                        "guardrails",
                    )
            return out

        return Tool(
            name=tool.name,
            description=tool.description,
            args_schema=tool.args_schema,
            executor=wrap_executor(inner, execute),
            raw_input_schema=tool.raw_input_schema,
            is_peer=tool.is_peer,
            source=tool.source,
            fatal=tool.fatal,
        )

    return _wrap_tools(tools, wrap_one)


def wrap_final_response_judges(agent: Agent, guardrails: Any, manifest_id: str) -> Agent:
    """Apply final_response=True judges to the agent's reply."""
    _ = manifest_id
    judges = [
        j for j in getattr(guardrails, "judges", []) if getattr(j, "final_response", False)
    ]
    if not judges:
        return agent

    from collections.abc import AsyncIterator

    from felix.patterns.types import ChatMessage, Event, InvokeInput, InvokeOutput

    class _FinalJudgeAgent:
        def __init__(self, inner: Agent) -> None:
            self._inner = inner
            for attr in (
                "tools",
                "pattern",
                "manifest_id",
                "manifest_version",
                "system_prompt",
            ):
                if hasattr(inner, attr):
                    setattr(self, attr, getattr(inner, attr))

        async def invoke(self, input: InvokeInput) -> InvokeOutput:
            result = await self._inner.invoke(input)
            content = result.final.content if result.final else ""
            for j in judges:
                score = _heuristic_judge_score(content, getattr(j, "criteria", "") or "")
                threshold = float(getattr(j, "threshold", 0.7) or 0.7)
                if score < threshold:
                    msg = ChatMessage(
                        role="assistant",
                        content=(
                            f"[judge denied] {j.name}: score={score:.2f} < {threshold}"
                        ),
                    )
                    return InvokeOutput(messages=[*result.messages[:-1], msg], final=msg)
            return result

        async def stream_events(self, input: InvokeInput) -> AsyncIterator[Event]:
            async for ev in self._inner.stream_events(input):
                yield ev

    return _FinalJudgeAgent(agent)  # type: ignore[return-value]


def apply_approvals(tools: list[Tool], rules: list[ApprovalRule], manifest_id: str) -> list[Tool]:
    gated: dict[str, ApprovalRule] = {}
    for r in rules:
        for name in r.tools:
            gated[name] = r
    if not gated:
        return tools

    def wrap_one(tool: Tool) -> Tool:
        rule = gated.get(tool.name)
        if rule is None:
            return tool
        inner = tool.executor

        async def execute(
            args: ToolInput, ctx: ToolInvocationCtx | None = None
        ) -> ToolOutput:
            import hashlib
            import json

            req = try_get_context()
            granted = bool((req.extras if req else {}).get(f"approval:{tool.name}"))
            if not granted and req is not None:
                try:
                    from felix.approvals import store as approvals_store

                    sig = hashlib.sha256(
                        json.dumps(args, sort_keys=True, default=str).encode()
                    ).hexdigest()[:32]
                    approved = await approvals_store.find_approved(
                        req.settings,
                        req.auth.tenant_id,
                        manifest_id=manifest_id,
                        tool_name=tool.name,
                        call_signature=sig,
                    )
                    if approved:
                        granted = True
                        if approved.get("edited_args"):
                            args = dict(approved["edited_args"])
                    else:
                        await approvals_store.create_pending(
                            req.settings,
                            req.auth.tenant_id,
                            manifest_id=manifest_id,
                            tool_name=tool.name,
                            call_signature=sig,
                            args=dict(args),
                            principal_subj=req.auth.principal_sub,
                            rule_id=rule.id,
                            ttl_seconds=rule.ttl_seconds,
                        )
                except Exception:
                    logger.debug("approvals store lookup failed", exc_info=True)

            if not granted:
                record_counter(
                    "felix_approval_required",
                    {"manifest_id": manifest_id, "tool": tool.name, "rule": rule.id},
                )
                return deny_output(
                    f"[approval required] tool={tool.name} rule={rule.id}",
                    "approvals",
                )
            return await inner.execute(args, ctx)

        return Tool(
            name=tool.name,
            description=tool.description,
            args_schema=tool.args_schema,
            executor=wrap_executor(inner, execute),
            raw_input_schema=tool.raw_input_schema,
            is_peer=tool.is_peer,
            source=tool.source,
            fatal=tool.fatal,
        )

    return _wrap_tools(tools, wrap_one)


async def _resolve_system_prompt(manifest: Manifest, deps: BuildDeps) -> str:
    sp = manifest.spec.system_prompt
    from felix.context_files import (
        load_agents_md_layer,
        load_instruction_files,
        load_system_md,
    )

    tenant_id = deps.tenant_id or (
        deps.auth.principal.tenant_id if deps.auth else "default"
    )

    # Pi SYSTEM.md: replace default prompt entirely when present.
    system_md = await load_system_md(
        sp.system_md,
        object_store=deps.object_store,
        workspace_root=deps.workspace_root,
        tenant_id=tenant_id,
    )
    if system_md:
        parts = [system_md]
    else:
        parts: list[str] = []
        if sp.soul and deps.soul_loader and deps.auth:
            try:
                soul = deps.soul_loader(deps.auth.principal.tenant_id)
                if hasattr(soul, "__await__"):
                    soul = await soul  # type: ignore[misc]
                if soul:
                    parts.append(str(soul))
            except Exception:
                logger.debug("soul loader failed", exc_info=True)
        if sp.base:
            parts.append(sp.base)
        if sp.inline:
            parts.append(sp.inline)

    if sp.files:
        file_parts = await load_instruction_files(
            file_keys=list(sp.files),
            object_store=deps.object_store,
            workspace_root=deps.workspace_root,
            tenant_id=tenant_id,
        )
        parts.extend(file_parts)

    if deps.load_agents_md or sp.files or sp.system_md or sp.append_system_md:
        # Also auto-discover AGENTS.md when any context-file feature is enabled.
        agents = await load_agents_md_layer(
            object_store=deps.object_store,
            workspace_root=deps.workspace_root,
            tenant_id=tenant_id,
            enabled=True,
        )
        if agents and not any(agents in (p or "") for p in parts):
            parts.append(agents)

    append_md = await load_system_md(
        sp.append_system_md,
        object_store=deps.object_store,
        workspace_root=deps.workspace_root,
        tenant_id=tenant_id,
    )
    if append_md:
        parts.append(append_md)

    return "\n\n---\n\n".join(p for p in parts if p)


def _collect_secrets(deps: BuildDeps) -> list[str]:
    from felix.secrets import collected_secret_values

    settings = deps.settings
    if settings is None:
        return collected_secret_values()
    return collected_secret_values(settings)


async def build_agent(
    manifest: Manifest | str | dict[str, Any],
    tools: ToolProvider | None = None,
    deps: BuildDeps | None = None,
    *,
    settings: Any | None = None,
) -> Agent:
    """Compile a manifest into a governance-wrapped Agent.

    Signature accepts ``build_agent(manifest, tools, deps)`` as specified;
    ``tools`` may also be supplied via ``deps.tools``.
    """
    if isinstance(manifest, str):
        try:
            m = load_bundled(manifest)
        except FileNotFoundError:
            m = parse_manifest({"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": manifest}})
    elif isinstance(manifest, dict):
        m = parse_manifest(manifest)
    else:
        m = manifest

    if deps is None:
        if tools is None:
            raise TypeError("build_agent requires tools or deps")
        deps = BuildDeps(tools=tools, settings=settings)
    elif tools is not None:
        deps.tools = tools
    if settings is not None:
        deps.settings = settings

    span = manifest_span(m.metadata.name, m.metadata.version)
    try:
        system_prompt = await _resolve_system_prompt(m, deps)
        tool_ids = list(m.spec.tools)

        sub_agents: dict[str, Agent] = {}
        if m.spec.sub_agents:
            builder = deps.sub_agent_builder or (
                lambda name: build_agent(name, deps=deps)
            )
            for name in m.spec.sub_agents:
                sub_agents[name] = await builder(name)

        resolved: list[Tool] = []
        if not m.spec.sub_agents:
            resolved = deps.tools.resolve(tool_ids)

        if deps.extra_tools:
            seen = {t.name for t in resolved}
            for t in deps.extra_tools:
                if t.name not in seen:
                    resolved.append(t)
                    seen.add(t.name)

        # Wire Agent Skills (progressive disclosure + bound skill tools).
        from felix.skills import (
            SKILL_TOOL_NAMES,
            get_skill_activation_store,
            load_manifest_skills,
            make_skill_tools,
            skill_catalog_xml,
        )

        tenant_id = deps.tenant_id or (
            deps.auth.principal.tenant_id if deps.auth else "default"
        )
        wants_skills = bool(m.spec.skills) or any(
            t.name in SKILL_TOOL_NAMES for t in resolved
        )
        if wants_skills:
            catalog = await load_manifest_skills(
                list(m.spec.skills),
                tenant_id=tenant_id,
                object_store=deps.object_store,
            )
            skill_tools = {
                t.name: t
                for t in make_skill_tools(
                    catalog,
                    activation_store=get_skill_activation_store(deps.settings),
                    tenant_id=tenant_id,
                    manifest_id=m.metadata.name,
                )
            }
            resolved = [skill_tools.get(t.name, t) for t in resolved]
            # Ensure skill tools exist when skills are declared but not listed.
            if m.spec.skills:
                have = {t.name for t in resolved}
                for name, tool in skill_tools.items():
                    if name not in have:
                        resolved.append(tool)
            catalog_block = skill_catalog_xml(catalog)
            if catalog_block:
                system_prompt = (
                    f"{system_prompt}\n\n---\n\n{catalog_block}"
                    if system_prompt
                    else catalog_block
                )

        # Governance pipeline (order matters — matches TS builder).
        resolved = apply_secret_masking(resolved, _collect_secrets(deps), m.metadata.name)
        if m.spec.policies:
            resolved = apply_policies(resolved, m.spec.policies, m.metadata.name)
        if m.spec.command_screening.enabled:
            resolved = apply_command_screening(
                resolved, m.spec.command_screening, m.metadata.name
            )
        if m.spec.content_screening.enabled:
            resolved = apply_content_screening(
                resolved, m.spec.content_screening, m.metadata.name
            )
        if any_limit(m.spec.limits):
            resolved = apply_limits(resolved, m.spec.limits, m.metadata.name)
        if guardrails_enabled(m.spec.guardrails):
            resolved = apply_guardrails(resolved, m.spec.guardrails, m.metadata.name)
        if judges_enabled(m.spec.guardrails):
            resolved = apply_judges(resolved, m.spec.guardrails, m.metadata.name)
        if m.spec.approvals:
            resolved = apply_approvals(resolved, m.spec.approvals, m.metadata.name)

        final_prompt = (
            system_prompt
            or f"You are {m.metadata.name}. Use your tools when needed to answer accurately."
        )

        pattern_builder = get_pattern(m.spec.pattern)
        if pattern_builder is None:
            raise ValueError(
                f"Unknown pattern '{m.spec.pattern}' for manifest '{m.metadata.name}' — "
                f"registered: {', '.join(list_patterns()) or '(none)'}"
            )

        agent = await pattern_builder(
            {
                "manifest": m,
                "model_spec": m.spec.model,
                "tools": resolved,
                "sub_agents": sub_agents,
                "system_prompt": final_prompt,
                "manifest_id": m.metadata.name,
                "manifest_version": m.metadata.version,
                "recursion_limit": m.spec.recursion_limit,
                "max_turns": m.spec.max_turns,
                "aggregator_prompt": m.spec.aggregator_prompt,
                "session_store": deps.session_store,
                "session_strategy": deps.session_strategy,
                "limits": m.spec.limits,
                "settings": deps.settings,
            }
        )
        if judges_enabled(m.spec.guardrails):
            agent = wrap_final_response_judges(
                agent, m.spec.guardrails, m.metadata.name
            )
        return agent
    finally:
        span.end()


__all__ = ["BuildDeps", "build_agent", "wrap_final_response_judges"]
