"""Compile a Manifest into a runnable Agent with governance wrappers."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from felix.auth.context import AuthContext
from felix.context import try_get_context
from felix.limits import effective_limits
from felix.manifests.loader import load_bundled, parse_manifest
from felix.manifests.schema import (
    ApprovalRule,
    Manifest,
    Policy,
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


def _append_unique_tools(resolved: list[Tool], extra: list[Tool]) -> None:
    seen = {t.name for t in resolved}
    for t in extra:
        if t.name not in seen:
            resolved.append(t)
            seen.add(t.name)


def apply_secret_masking(tools: list[Tool], secrets: list[str], manifest_id: str) -> list[Tool]:
    """Innermost: redact known secrets from tool output before anything else sees them."""
    if not secrets:
        return tools

    def wrap_one(tool: Tool) -> Tool:
        inner = tool.executor

        async def execute(args: ToolInput, ctx: ToolInvocationCtx | None = None) -> ToolOutput:
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

        async def execute(args: ToolInput, ctx: ToolInvocationCtx | None = None) -> ToolOutput:
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


# Argument names that carry something the host will execute. The screener read only
# "command"/"cmd", so the built-in sandbox tool — whose args are (code, path, stdin) and
# which runs ["python", "-c", code] — skipped every rule while *appearing* wrapped.
_COMMAND_ARG_KEYS = ("command", "cmd", "code", "script", "stdin", "argv", "shell_command", "args")

# For these transports the payload *is* the program, so every string argument is
# execution-bearing regardless of what the remote tool decided to call it.
_EXECUTION_TRANSPORTS = frozenset({"sandbox", "container"})


def _screenable_command_text(args: ToolInput, transport: str) -> str:
    """Concatenate the argument values command screening should inspect."""

    def flatten(value: Any) -> list[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, (list, tuple)):
            return [str(v) for v in value if isinstance(v, (str, int, float))]
        return []

    if transport in _EXECUTION_TRANSPORTS:
        parts = [p for v in args.values() for p in flatten(v)]
    else:
        parts = [p for k in _COMMAND_ARG_KEYS if k in args for p in flatten(args[k])]
    return "\n".join(p for p in parts if p)


def apply_command_screening(tools: list[Tool], screening: Any, manifest_id: str) -> list[Tool]:
    if not getattr(screening, "enabled", False):
        return tools
    import re

    rules = list(getattr(screening, "rules", []) or [])
    if getattr(screening, "include_defaults", True):
        from felix.manifests.schema import CommandRule

        existing = {r.pattern for r in rules}
        for pattern, decision, reason in _DEFAULT_COMMAND_RULES:
            if pattern not in existing:
                rules.append(CommandRule(pattern=pattern, decision=decision, reason=reason))

    compiled = [(re.compile(r.pattern, re.I), r.decision, r.reason or r.pattern) for r in rules]
    targets = set(getattr(screening, "target_tools", []) or [])

    def wrap_one(tool: Tool) -> Tool:
        if targets and tool.name not in targets:
            if tool.executor.transport not in {"sandbox", "container"}:
                return tool
        if not compiled and tool.executor.transport not in {"sandbox", "container"}:
            return tool
        inner = tool.executor

        async def execute(args: ToolInput, ctx: ToolInvocationCtx | None = None) -> ToolOutput:
            cmd = _screenable_command_text(args, tool.executor.transport)
            if cmd and compiled:
                for rx, decision, reason in compiled:
                    if rx.search(cmd):
                        if decision == "deny":
                            return deny_output(
                                f"[command denied] {reason}",
                                "command",  # type: ignore[arg-type]
                            )
                        if decision == "require_approval":
                            record_counter(
                                "felix_approval_required",
                                {"manifest_id": manifest_id, "tool": tool.name, "rule": "command"},
                            )
                            ok, args, note = await _await_approval(
                                manifest_id=manifest_id,
                                tool_name=tool.name,
                                rule_id=f"command:{reason}",
                                args=args,
                                ctx=ctx,
                                ttl_seconds=int(getattr(screening, "approval_ttl_seconds", 300) or 300),
                                reason=reason,
                            )
                            if not ok:
                                return deny_output(
                                    f"[command approval {note or 'denied'}] {reason}",
                                    "approvals",
                                )
                            break
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


# Default deny/approval patterns when command_screening.include_defaults is true.
_DEFAULT_COMMAND_RULES: tuple[tuple[str, str, str], ...] = (
    (r"rm\s+(-[a-zA-Z]*f[a-zA-Z]*\s+)*(/|~|/etc|/var|/usr|/home)", "deny", "destructive rm"),
    (r"\bmkfs\b|\bdd\s+if=", "deny", "disk wipe"),
    (r":\(\)\s*\{\s*:\|:&\s*\}\s*;", "deny", "fork bomb"),
    (r"\bcurl\b.*\|\s*(ba)?sh\b|\bwget\b.*\|\s*(ba)?sh\b", "deny", "pipe remote shell"),
    (r"\bchmod\s+(-R\s+)?777\b", "require_approval", "world-writable chmod"),
    (r"\bsudo\b|\bsu\s", "require_approval", "privilege escalation"),
)


_INJECTION_MARKERS = (
    "ignore previous instructions",
    "ignore your previous instructions",
    "disregard your instructions",
    "system prompt",
)

_UNTRUSTED_TRANSPORTS = frozenset({"mcp", "a2a", "browser", "container", "sandbox", "queue"})


def _is_untrusted_tool(tool: Tool) -> bool:
    if tool.executor.transport in _UNTRUSTED_TRANSPORTS:
        return True
    source = tool.source or ""
    return source.startswith(("mcp", "peer", "a2a", "queue", "browser"))


def apply_content_screening(tools: list[Tool], screening: Any, manifest_id: str) -> list[Tool]:
    if not getattr(screening, "enabled", False):
        return tools
    on_flag = getattr(screening, "on_flag", "quarantine")
    named = set(getattr(screening, "tools", []) or [])
    model_id = (getattr(screening, "model", "") or "").strip()

    def wrap_one(tool: Tool) -> Tool:
        if named and tool.name not in named:
            return tool
        if not named and not _is_untrusted_tool(tool):
            return tool
        inner = tool.executor

        async def execute(args: ToolInput, ctx: ToolInvocationCtx | None = None) -> ToolOutput:
            out = await inner.execute(args, ctx)
            if is_wrapper_deny(out):
                return out
            content = tool_output_content(out)
            lower = content.lower()
            flagged = any(m in lower for m in _INJECTION_MARKERS)
            unavailable = False
            if not flagged and model_id:
                from felix.config import get_settings
                from felix.governance.inbound import screen_for_injection

                result = await screen_for_injection(get_settings(), content, model_id)
                # Unavailable is not clean: this is the path that screens MCP, A2A,
                # browser and sandbox output, so failing open here is the whole ballgame.
                unavailable = result.unavailable
                flagged = result.flagged
            if unavailable:
                record_counter(
                    "felix_content_screening",
                    {"manifest_id": manifest_id, "tool": tool.name, "action": "unavailable"},
                )
                if on_flag == "block":
                    return deny_output(
                        "[screening unavailable] tool output could not be screened",
                        "screening",  # type: ignore[arg-type]
                    )
                return _replace_content(out, "[quarantined] tool output could not be screened")
            if flagged:
                record_counter(
                    "felix_content_screening",
                    {"manifest_id": manifest_id, "tool": tool.name, "action": on_flag},
                )
                if on_flag == "block":
                    return deny_output("[screening blocked] untrusted content", "screening")  # type: ignore[arg-type]
                notice = "[quarantined] tool output flagged as potentially hostile"
                # ToolOutput includes plain dict; `out.content = ...` raised
                # AttributeError there, so quarantine silently became "tool crashed".
                return _replace_content(out, notice)
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


async def _await_approval(
    *,
    manifest_id: str,
    tool_name: str,
    rule_id: str,
    args: ToolInput,
    ctx: ToolInvocationCtx | None,
    ttl_seconds: int | None,
    reason: str = "",
) -> tuple[bool, ToolInput, str]:
    """Create a pending approval, notify listeners, and block on the decision.

    Returns ``(approved, args, note)`` — ``args`` may be replaced by the approver's
    edits. Fails closed: no request context, no approvals store, or a store error all
    yield ``approved=False``.
    """
    import hashlib
    import json

    from felix.approvals.interrupt import wait_for_decision
    from felix.side_events import emit as emit_side_event

    req = try_get_context()
    if req is None:
        return False, args, "no request context"

    try:
        from felix.approvals import store as approvals_store

        sig = hashlib.sha256(json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()[:32]
        pending_row = await approvals_store.create_pending(
            req.settings,
            req.auth.tenant_id,
            manifest_id=manifest_id,
            tool_name=tool_name,
            call_signature=sig,
            args=dict(args),
            principal_subj=req.auth.principal_sub,
            rule_id=rule_id,
            ttl_seconds=ttl_seconds,
        )
    except Exception:
        logger.debug("approvals store create_pending failed", exc_info=True)
        return False, args, "approvals unavailable"

    approval_id = str(pending_row.get("id") or "")
    thread_id = (ctx.thread_id if ctx else None) or req.thread_id
    await emit_side_event(
        thread_id,
        "approval_required",
        {
            "approval_id": approval_id,
            "tool_name": tool_name,
            "args": dict(args),
            "rule_id": rule_id,
            "reason": reason,
            "thread_id": thread_id,
            "tool_call_id": ctx.tool_call_id if ctx else None,
        },
    )
    decision = await wait_for_decision(
        approval_id,
        timeout=float(ttl_seconds) if ttl_seconds else None,
    )
    if decision.decision != "approved":
        return False, args, decision.note or "denied"
    if decision.edited_args:
        args = dict(decision.edited_args)
    return True, args, ""


def apply_limits(tools: list[Tool], limits: Any, manifest_id: str) -> list[Tool]:
    def wrap_one(tool: Tool) -> Tool:
        inner = tool.executor

        async def execute(args: ToolInput, ctx: ToolInvocationCtx | None = None) -> ToolOutput:
            from felix.limits import check_budgets, trip

            req = try_get_context()
            if req is None:
                # Fail closed. A limits wrapper that silently does nothing when there is
                # no request context is exactly the hole this phase exists to close.
                return deny_output("[limits] no request context; refusing to run unbudgeted", "limits")

            ls = req.limit_state
            if ls.aborted:
                return deny_output(f"[limits] run aborted: {ls.abort_reason or 'budget exceeded'}", "limits")

            verdict = check_budgets(limits, ls)
            if verdict.exceeded:
                trip(ls, verdict.reason)
                return deny_output(f"[limits] {verdict.reason}", "limits")

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


def apply_guardrails(tools: list[Tool], guardrails: Any, manifest_id: str) -> list[Tool]:
    providers = set(getattr(guardrails, "providers", []) or [])
    if "pii" not in providers:
        return tools
    block = bool(getattr(guardrails, "block_on_match", False))
    targets = set(getattr(guardrails, "targets", []) or [])
    # Default targets include output; skip wrapping when only input is requested.
    if targets and "output" not in targets and "final_response" not in targets:
        return tools

    def wrap_one(tool: Tool) -> Tool:
        inner = tool.executor

        async def execute(args: ToolInput, ctx: ToolInvocationCtx | None = None) -> ToolOutput:
            out = await inner.execute(args, ctx)
            if is_wrapper_deny(out):
                return out
            content = tool_output_content(out)
            from felix.governance.pii import redact_pii

            result = redact_pii(content)
            if result.matched and block:
                return deny_output("[guardrails] PII blocked", "guardrails")
            return _replace_content(out, result.text)

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
    # Explicit assertions, so a criterion's *polarity* is stated rather than inferred.
    for prefix in ("assert_absent:", "must_not_contain:"):
        if c.startswith(prefix):
            needles = [n.strip() for n in c.split(":", 1)[1].split(",") if n.strip()]
            lower = text.lower()
            return 0.0 if any(n in lower for n in needles) else 1.0
    for prefix in ("assert_present:", "must_contain:"):
        if c.startswith(prefix):
            needles = [n.strip() for n in c.split(":", 1)[1].split(",") if n.strip()]
            lower = text.lower()
            return 1.0 if all(n in lower for n in needles) else 0.0

    # Bag-of-words overlap cannot express a negative criterion: for
    # "must not leak credentials or secrets" it scored output *containing* those words
    # highest, so a safety judge passed exactly what it was meant to block. Refuse to
    # guess — a judge with no model and no explicit assertion fails closed.
    if _looks_negated(c):
        logger.error(
            "judge criteria %r is a negative assertion but has no model and no "
            "assert_absent: prefix; failing closed",
            criteria,
        )
        return 0.0

    tokens = [t for t in c.replace(",", " ").split() if len(t) > 2]
    if not tokens:
        return 1.0 if len(text) >= 3 else 0.0
    lower = text.lower()
    words = set(lower.split())
    hit = sum(1 for t in tokens if t in words or t in lower)
    return hit / len(tokens)


_NEGATION_MARKERS = (
    "must not",
    "should not",
    "never",
    "no ",
    "without",
    "avoid",
    "free of",
    "does not",
    "doesn't",
    "don't",
    "cannot",
    "refuse",
    "prohibit",
    "forbid",
)


def _looks_negated(criteria: str) -> bool:
    """True when a criterion reads as "must NOT ..." — polarity a bag of words inverts."""
    return any(m in criteria for m in _NEGATION_MARKERS)


async def _judge_score(content: str, judge: Any, *, settings: Any | None = None) -> float:
    """Heuristic score, or LLM score when ``judge.model`` is set."""
    criteria = getattr(judge, "criteria", "") or ""
    model_id = (getattr(judge, "model", "") or "").strip()
    if not model_id or settings is None:
        return _heuristic_judge_score(content, criteria)
    try:
        from felix.eval.compare import llm_judge_score
        from felix.manifests.schema import ModelSpec
        from felix.patterns.model import build_model

        model = build_model(settings, ModelSpec(id=model_id))
        result = await llm_judge_score(
            model,
            user_input="",
            answer=content,
            criteria=criteria,
            threshold=float(getattr(judge, "threshold", 0.7) or 0.7),
        )
        return float(result.get("score") or 0.0)
    except Exception:
        return _heuristic_judge_score(content, criteria)


def apply_judges(tools: list[Tool], guardrails: Any, manifest_id: str) -> list[Tool]:
    """Apply tool-output judges (heuristic, or LLM when JudgeRule.model is set)."""
    _ = manifest_id
    judges = [j for j in getattr(guardrails, "judges", []) if not getattr(j, "final_response", False)]
    if not judges:
        return tools

    def wrap_one(tool: Tool) -> Tool:
        applicable = [j for j in judges if not j.target_tools or tool.name in j.target_tools]
        if not applicable:
            return tool
        inner = tool.executor

        async def execute(args: ToolInput, ctx: ToolInvocationCtx | None = None) -> ToolOutput:
            out = await inner.execute(args, ctx)
            if is_wrapper_deny(out):
                return out
            content = tool_output_content(out)
            from felix.config import get_settings

            settings = get_settings()
            for j in applicable:
                score = await _judge_score(content, j, settings=settings)
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
    judges = [j for j in getattr(guardrails, "judges", []) if getattr(j, "final_response", False)]
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
            from felix.config import get_settings

            settings = get_settings()
            for j in judges:
                score = await _judge_score(content, j, settings=settings)
                threshold = float(getattr(j, "threshold", 0.7) or 0.7)
                if score < threshold:
                    msg = ChatMessage(
                        role="assistant",
                        content=(f"[judge denied] {j.name}: score={score:.2f} < {threshold}"),
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

        async def execute(args: ToolInput, ctx: ToolInvocationCtx | None = None) -> ToolOutput:
            import hashlib
            import json

            from felix.approvals.interrupt import wait_for_decision
            from felix.side_events import emit as emit_side_event

            req = try_get_context()
            granted = bool((req.extras if req else {}).get(f"approval:{tool.name}"))
            pending_row: dict[str, object] | None = None
            if not granted and req is not None:
                try:
                    from felix.approvals import store as approvals_store

                    sig = hashlib.sha256(json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()[
                        :32
                    ]
                    approved = await approvals_store.find_approved(
                        req.settings,
                        req.auth.tenant_id,
                        manifest_id=manifest_id,
                        tool_name=tool.name,
                        call_signature=sig,
                        # bind_principal: A's approval must not authorize B's call.
                        principal_subj=(req.auth.principal_sub or "") if rule.bind_principal else None,
                        # one_shot: a spent grant must not authorize a replay.
                        unconsumed_only=bool(rule.one_shot),
                    )
                    if approved:
                        if rule.one_shot and not await approvals_store.consume_approval(
                            req.settings, req.auth.tenant_id, str(approved.get("id") or "")
                        ):
                            # Lost the race to another call spending the same grant.
                            approved = None
                    if approved:
                        granted = True
                        if approved.get("edited_args"):
                            args = dict(approved["edited_args"])
                    else:
                        pending_row = await approvals_store.create_pending(
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
                if pending_row is None:
                    return deny_output(
                        f"[approval required] tool={tool.name} rule={rule.id}",
                        "approvals",
                    )

                approval_id = str(pending_row.get("id") or "")
                thread_id = (ctx.thread_id if ctx else None) or (req.thread_id if req else None)
                await emit_side_event(
                    thread_id,
                    "approval_required",
                    {
                        "approval_id": approval_id,
                        "tool_name": tool.name,
                        "args": dict(args),
                        "rule_id": rule.id,
                        "thread_id": thread_id,
                        "tool_call_id": ctx.tool_call_id if ctx else None,
                    },
                )
                decision = await wait_for_decision(
                    approval_id,
                    timeout=float(rule.ttl_seconds) if rule.ttl_seconds else None,
                )
                if decision.decision != "approved":
                    note = decision.note or "denied"
                    return deny_output(
                        f"[approval {note}] tool={tool.name} rule={rule.id}",
                        "approvals",
                    )
                if decision.edited_args:
                    args = dict(decision.edited_args)
                return await inner.execute(args, ctx)
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

    tenant_id = deps.tenant_id or (deps.auth.principal.tenant_id if deps.auth else "default")

    # SYSTEM.md: replace default prompt entirely when present.
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
            builder = deps.sub_agent_builder or (lambda name: build_agent(name, deps=deps))
            for name in m.spec.sub_agents:
                sub_agents[name] = await builder(name)

        resolved: list[Tool] = []
        if not m.spec.sub_agents:
            resolved = deps.tools.resolve(tool_ids)

        if deps.extra_tools:
            _append_unique_tools(resolved, deps.extra_tools)

        tenant_id = deps.tenant_id or (deps.auth.principal.tenant_id if deps.auth else "default")
        allow_http = bool(
            deps.settings
            and getattr(deps.settings, "environment", "") == "development"
            and getattr(deps.settings, "allow_insecure", False)
        )

        # Governance compile checks (frameworks + plaintext secret policy).
        from felix.manifests.governance import apply_transparency_notice, validate_governance
        from felix.manifests.secret_refs import resolve_outbound_secrets

        validate_governance(m, deps.settings)
        if m.spec.governance.transparency_notice:
            system_prompt = apply_transparency_notice(system_prompt or "", m.metadata.name)

        mcp_refs, peer_refs, container_refs = await resolve_outbound_secrets(m, deps.settings)

        # Outbound MCP servers → remote tools.
        if mcp_refs:
            try:
                from felix.mcp.client import tools_from_mcp_servers

                mcp_tools = await tools_from_mcp_servers(mcp_refs, allow_http=allow_http)
                _append_unique_tools(resolved, mcp_tools)
            except Exception:
                logger.warning("MCP client tool binding failed", exc_info=True)

        # A2A peers → peer__{name} tools.
        if peer_refs:
            try:
                from felix.a2a.peers import tools_from_peers

                peer_tools = tools_from_peers(peer_refs, allow_http=allow_http)
                _append_unique_tools(resolved, peer_tools)
            except Exception:
                logger.warning("peer tool binding failed", exc_info=True)

        # Playwright browser tools (optional extra).
        if m.spec.browser_tools:
            try:
                from felix.tools.browser import tools_from_browser_refs

                _append_unique_tools(
                    resolved,
                    tools_from_browser_refs(list(m.spec.browser_tools), allow_http=allow_http),
                )
            except Exception:
                logger.warning("browser tool binding failed", exc_info=True)

        # Client-executed tools (browser/desktop float posts results back).
        if m.spec.client_tools:
            try:
                from felix.tools.client_bridge import tools_from_client_refs

                _append_unique_tools(resolved, tools_from_client_refs(list(m.spec.client_tools)))
            except Exception:
                logger.warning("client tool binding failed", exc_info=True)

        # Docker sandboxes + HTTP container gateways.
        if m.spec.sandboxes:
            try:
                from felix.tools.sandboxes import tools_from_sandboxes

                _append_unique_tools(resolved, tools_from_sandboxes(list(m.spec.sandboxes)))
            except Exception:
                logger.warning("sandbox tool binding failed", exc_info=True)

        if container_refs:
            try:
                from felix.tools.sandboxes import tools_from_containers

                _append_unique_tools(
                    resolved,
                    tools_from_containers(container_refs, allow_http=allow_http),
                )
            except Exception:
                logger.warning("container tool binding failed", exc_info=True)

        if m.spec.queues:
            try:
                from felix.tools.queues import tools_from_queues

                _append_unique_tools(
                    resolved,
                    tools_from_queues(list(m.spec.queues), settings=deps.settings),
                )
            except Exception:
                logger.warning("queue tool binding failed", exc_info=True)

        # Procedural memory write tool (retrieve happens per turn in ReAct).
        if m.spec.procedural_memory.enabled and deps.settings is not None:
            try:
                from felix.memory.procedural import make_remember_procedure_tool

                _append_unique_tools(
                    resolved,
                    [
                        make_remember_procedure_tool(
                            settings=deps.settings,
                            tenant_id=tenant_id,
                            manifest_id=m.metadata.name,
                        )
                    ],
                )
            except Exception:
                logger.warning("procedural memory tool binding failed", exc_info=True)

        # Wire Agent Skills (progressive disclosure + bound skill tools).
        from felix.skills import (
            SKILL_TOOL_NAMES,
            get_skill_activation_store,
            load_manifest_skills,
            make_skill_tools,
            skill_catalog_xml,
        )

        wants_skills = bool(m.spec.skills) or any(t.name in SKILL_TOOL_NAMES for t in resolved)
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
                    f"{system_prompt}\n\n---\n\n{catalog_block}" if system_prompt else catalog_block
                )

        # Recalled facts, rendered as a per-run prelude rather than folded into the
        # system prompt. Empty when memory is disabled or has nothing stored.
        context_prelude = ""

        # Inject active long-term facts when memory capture/store is enabled.
        memory_capture = m.spec.memory.capture
        if (
            deps.settings is not None
            and m.spec.memory.store != "none"
            and (memory_capture.enabled or m.spec.memory.store in {"pgvector", "memory"})
        ):
            try:
                from felix.memory.capture import active_facts_prompt

                # Deliberately NOT appended to the system prompt. Two reasons:
                #
                # 1. Cache. Anthropic renders tools -> system -> messages and caching is a
                #    prefix match, so anything that changes invalidates everything after
                #    it. This block changes whenever memory capture writes a fact, which
                #    is often, so folding it into `system` meant the cache breakpoint sat
                #    on a prefix that moved every turn — cache_read_input_tokens near zero.
                # 2. Trust. These facts are model-extracted from earlier turns and can
                #    carry text that originated in tool output. They are reference
                #    material, not developer-tier instructions.
                context_prelude = await active_facts_prompt(
                    deps.settings,
                    tenant_id,
                    manifest_id=m.metadata.name,
                )
            except Exception:
                logger.debug("active facts inject failed", exc_info=True)

        # Governance pipeline (order matters — matches TS builder).
        resolved = apply_secret_masking(resolved, _collect_secrets(deps), m.metadata.name)
        if m.spec.policies:
            resolved = apply_policies(resolved, m.spec.policies, m.metadata.name)
        if m.spec.command_screening.enabled:
            resolved = apply_command_screening(resolved, m.spec.command_screening, m.metadata.name)
        if m.spec.content_screening.enabled:
            resolved = apply_content_screening(resolved, m.spec.content_screening, m.metadata.name)
        # Always installed. Previously gated on any_limit(), so a manifest that declared
        # no limits got no tool-call cap, no wall clock, no token or spend ceiling —
        # and the wrapper silently did nothing when there was no request context.
        resolved = apply_limits(resolved, effective_limits(m.spec.limits), m.metadata.name)
        if guardrails_enabled(m.spec.guardrails):
            resolved = apply_guardrails(resolved, m.spec.guardrails, m.metadata.name)
        if judges_enabled(m.spec.guardrails):
            resolved = apply_judges(resolved, m.spec.guardrails, m.metadata.name)
        if m.spec.approvals:
            resolved = apply_approvals(resolved, m.spec.approvals, m.metadata.name)

        if m.spec.artifacts.enabled:
            from felix.artifacts import apply_artifact_spill

            resolved = apply_artifact_spill(
                resolved,
                m.spec.artifacts,
                object_store=deps.object_store,
                tenant_id=tenant_id,
                manifest_id=m.metadata.name,
            )

        final_prompt = (
            system_prompt or f"You are {m.metadata.name}. Use your tools when needed to answer accurately."
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
                # Volatile, per-run reference material. Kept out of `system` so the
                # cached prefix stays stable across turns.
                "context_prelude": context_prelude,
                "manifest_id": m.metadata.name,
                "manifest_version": m.metadata.version,
                "recursion_limit": m.spec.recursion_limit,
                "max_turns": m.spec.max_turns,
                "aggregator_prompt": m.spec.aggregator_prompt,
                "session_store": deps.session_store,
                "session_strategy": deps.session_strategy,
                "session_spec": m.spec.session,
                "execution": m.spec.execution,
                "limits": effective_limits(m.spec.limits),
                "settings": deps.settings,
                "tenant_id": tenant_id,
                "memory_capture": m.spec.memory.capture,
                "tools_retrieval": m.spec.tools_retrieval,
                "procedural_memory": m.spec.procedural_memory,
            }
        )
        if judges_enabled(m.spec.guardrails):
            agent = wrap_final_response_judges(agent, m.spec.guardrails, m.metadata.name)
        return agent
    finally:
        span.end()


__all__ = ["BuildDeps", "build_agent", "wrap_final_response_judges"]
