"""Compile a Manifest into a runnable Agent with governance wrappers."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from felix.auth.context import AuthContext
from felix.context import try_get_context
from felix.decisions import MeteredDecider
from felix.governance.content_screening import _INJECTION
from felix.governance.image_screening import ImageScreener, screen_session_strategy
from felix.governance.inbound import replay_screener, tool_image_screener
from felix.governance.judges import judge_score
from felix.governance.reply import ReplyScreen, screen_session_store
from felix.limits import EffectiveLimits, effective_limits
from felix.manifests.loader import load_bundled, parse_manifest
from felix.manifests.schema import (
    ApprovalRule,
    CommandScreening,
    ContentScreening,
    DeciderSpec,
    Guardrails,
    Limits,
    Manifest,
    Policy,
    guardrails_enabled,
    judges_enabled,
)
from felix.manifests.tool_match import matches_any, unmatched_patterns
from felix.observability.metrics import record_counter
from felix.observability.tracing import manifest_span
from felix.patterns.registry import get_pattern, honours_output_schema, list_patterns
from felix.patterns.types import Agent
from felix.skills.types import SkillCatalog
from felix.tools.executor import wrap_executor
from felix.tools.provider import ToolProvider
from felix.tools.types import (
    Tool,
    ToolInput,
    ToolInvocationCtx,
    ToolOutput,
    deny_output,
    is_untrusted_output,
    is_wrapper_deny,
    output_metadata,
    replace_tool_output,
    tool_output_content,
    tool_output_images,
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
    # Manifests whose sub-agents are being compiled right now, outermost first. A router that
    # names a router that names the first is a cycle, and without this it recursed until
    # Python's stack gave out — now that tenants author routers, one row can do that.
    compiling: list[str] = field(default_factory=list)
    # Children already compiled in this build, by name. Without it a stored A → [B, C], both
    # naming D, compiled D twice — and a tenant's A → 100 x B → 100 x C is 10^4 compiles, each
    # with object-store reads and an MCP `list_tools` per server, for one chat.
    compiled: dict[str, Agent] = field(default_factory=dict)
    # The reply screen of the compile whose sub-agents are being built, so a child's own
    # screen chains to it (`ReplyScreen.parent`). Set and restored around the child compile.
    reply_screen: Any | None = None


# Routers of routers of routers, and no further. A bound on nesting, beside the memo above, is
# what keeps the compile a request triggers proportional to what the tenant meant to write.
MAX_SUB_AGENT_DEPTH = 4


@contextmanager
def _compiling_children(deps: BuildDeps, name: str, **inherited: Any) -> Iterator[None]:
    """While `name`'s children compile: on the stack, with what they inherit from it set.

    Each value in `inherited` replaces the `BuildDeps` field of that name and is restored on
    the way out, so one sibling's compile cannot leak into the next. One place for the rule:
    the fields had grown to three hand-written save-and-restore pairs, and a missed restore
    fails nothing — it quietly hands a child's store or screen to whatever compiles next.
    Not `dataclasses.replace`: `compiled` and `compiling` must stay shared across the tree.
    """
    if name in deps.compiling:
        raise ValueError(f"sub_agents form a cycle: {' -> '.join([*deps.compiling, name])}")
    if len(deps.compiling) >= MAX_SUB_AGENT_DEPTH:
        chain = " -> ".join([*deps.compiling, name])
        raise ValueError(f"sub_agents nest deeper than {MAX_SUB_AGENT_DEPTH}: {chain}")
    saved = {field_name: getattr(deps, field_name) for field_name in inherited}
    deps.compiling.append(name)
    for field_name, value in inherited.items():
        setattr(deps, field_name, value)
    try:
        yield
    finally:
        for field_name, value in saved.items():
            setattr(deps, field_name, value)
        deps.compiling.pop()


def _bundled_sub_agent(deps: BuildDeps) -> Callable[[str], Awaitable[Agent]]:
    """Compile a sub-agent from bundled YAML — the resolver for callers with no tenant.

    Unknown names raise. This used to be `build_agent(name)`, which turns a name it cannot
    find into an empty manifest — so a missing child compiled to `You are <name>.` with no
    tools, and the router sent requests to it without a word.
    """

    async def build(name: str) -> Agent:
        try:
            child = load_bundled(name)
        except FileNotFoundError as exc:
            raise LookupError(f"Unknown sub-agent manifest: {name}") from exc
        return await build_agent(child, deps=deps)

    return build


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


def _bind_artifact_reader(resolved: list[Tool], m: Any, deps: BuildDeps, tenant_id: str) -> None:
    """Bind `read_artifact` beside `spec.artifacts`, before the governance stack.

    Before it, so a read is limited, screened and audited like the tool call that produced the
    artifact. Only with a store: without one the spill is a no-op and there is nothing to read.
    """
    if not m.spec.artifacts.enabled or deps.object_store is None:
        return
    from felix.artifacts import READ_ARTIFACT_TOOL, make_read_artifact_tool

    if any(t.name == READ_ARTIFACT_TOOL for t in resolved):
        # Not refused: the manifest's tool may be deliberate. But the spill marker's reader is
        # now that tool, and nothing else would say so.
        logger.warning(
            "manifest %s binds its own %r; spilled outputs cannot be read back by the model",
            m.metadata.name,
            READ_ARTIFACT_TOOL,
        )
    _append_unique_tools(
        resolved,
        [
            make_read_artifact_tool(
                m.spec.artifacts,
                object_store=deps.object_store,
                tenant_id=tenant_id,
                manifest_id=m.metadata.name,
            )
        ],
    )


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
            return replace_tool_output(out, content=content)

        return _clone_tool(tool, wrap_executor(inner, execute))

    return _wrap_tools(tools, wrap_one)


async def _screen_tool_images(out: ToolOutput, screener: ImageScreener, tool_name: str) -> ToolOutput:
    """`out` with each image the screener refuses removed and its note added to the text."""
    from felix_ai.types import ChatMessage

    images = tool_output_images(out)
    content = tool_output_content(out)
    screened = await screener.screen(
        ChatMessage(role="tool", name=tool_name, content=content, attachments=images)
    )
    if screened.attachments == images:
        return out
    return replace_tool_output(out, content=screened.content, images=list(screened.attachments or ()))


def tool_guidance_section(tools: list[Tool], by_name: dict[str, str]) -> str:
    """The system prompt's tool guidance: one line per line of guidance, for present tools only.

    From two places — a tool's own `prompt_guidance`, and `spec.tool_guidance` keyed by name or
    glob — in the order the tools resolved, each line once. Built from the tools the agent has
    after the compile, so guidance for a tool that is not bound never reaches the prompt: the
    drift that hand-written tool advice in `system_prompt` accumulates.
    """
    lines: list[str] = []
    for tool in tools:
        own = [tool.prompt_guidance] if tool.prompt_guidance else []
        declared = [line for pattern, line in by_name.items() if matches_any([pattern], tool.name)]
        for line in (*own, *declared):
            text = " ".join(line.split())
            if text and text not in lines:
                lines.append(text)
    if not lines:
        return ""
    return "Tool guidance:\n" + "\n".join(f"- {line}" for line in lines)


def _clone_tool(tool: Tool, executor: Any) -> Tool:
    """Copy a tool with a new executor, carrying every other field forward.

    `dataclasses.replace` rather than a field-by-field rebuild on purpose: every tool
    passes through this on its way through the governance stack, so a field that the
    rebuild forgot would be silently reset to its default on every wrapped tool — and the
    default is what a field means when nobody has thought about it. `replay_safe` was
    added and very nearly lost exactly that way.
    """
    return replace(tool, executor=executor)


def apply_policies(tools: list[Tool], policies: list[Policy], manifest_id: str) -> list[Tool]:
    def wrap_one(tool: Tool) -> Tool:
        # Matched per tool rather than through a dict keyed by literal name, which cannot
        # express a pattern. Order is the manifest's, so a denial names the first rule that
        # refuses, as it did before.
        rules = [p for p in policies if matches_any(p.tools, tool.name)]
        if not rules:
            return tool
        inner = tool.executor

        async def execute(args: ToolInput, ctx: ToolInvocationCtx | None = None) -> ToolOutput:
            req_ctx = try_get_context()
            # `frozenset(...)`, not the attribute as-is. `AuthContext.scopes` is an unvalidated
            # dataclass field, and the plugin authenticator seam adopts whatever `Principal` a
            # plugin returns — a plugin doing `scopes=" ".join(claims["scope"])` yields a `str`,
            # for which `s not in scopes` is a *substring* test: `tools:calc` is "held" by a
            # caller with `tools:calculator`, and `admin` by one with `no-admin`. Coercing here
            # makes the check a membership test whatever the caller layer produced.
            scopes = frozenset(req_ctx.auth.scopes) if req_ctx else frozenset()
            for rule in rules:
                # Fail closed on a rule that requires nothing. The schema rejects this shape,
                # so reaching it means a Policy was built in code or parsed by an older path —
                # and `[s for s in [] if ...]` is empty, which would read as "every scope
                # satisfied" and permit the call. Same stance as apply_limits with no context.
                if not rule.required_scopes:
                    record_counter(
                        "felix_policy_deny",
                        {"manifest_id": manifest_id, "tool": tool.name, "policy": rule.id},
                    )
                    return deny_output(
                        f"[policy denied] {rule.id} requires no scopes, so it cannot authorise {tool.name}",
                        "policy",
                    )
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

        return _clone_tool(tool, wrap_executor(inner, execute))

    return _wrap_tools(tools, wrap_one)


# Argument names that carry something the host will execute. The screener read only
# "command"/"cmd", so the built-in sandbox tool — whose args are (code, path, stdin) and
# which runs ["python", "-c", code] — skipped every rule while *appearing* wrapped.
_COMMAND_ARG_KEYS = ("command", "cmd", "code", "script", "stdin", "argv", "shell_command", "args")

# For these transports the payload *is* the program, so every string argument is
# execution-bearing regardless of what the remote tool decided to call it.
_EXECUTION_TRANSPORTS = frozenset({"sandbox", "container", "shell"})


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


def apply_command_screening(
    tools: list[Tool], screening: CommandScreening | None, manifest_id: str
) -> list[Tool]:
    if screening is None or not screening.enabled:
        return tools
    import re

    rules = list(screening.rules)
    if screening.include_defaults:
        from felix.manifests.schema import CommandRule

        existing = {r.pattern for r in rules}
        for pattern, decision, reason in _DEFAULT_COMMAND_RULES:
            if pattern not in existing:
                rules.append(CommandRule(pattern=pattern, decision=decision, reason=reason))

    compiled = [(re.compile(r.pattern, re.I), r.decision, r.reason or r.pattern) for r in rules]
    targets = list(screening.target_tools)

    def wrap_one(tool: Tool) -> Tool:
        # An execution transport is screened whatever `target_tools` says and even with no
        # rules compiled: for those the payload *is* the program. One set, `_EXECUTION_TRANSPORTS`,
        # decides that here and in `_screenable_command_text` — it was a literal in both of
        # these lines once, and the shell transport was added to the set and not to them.
        if targets and not matches_any(targets, tool.name):
            if tool.executor.transport not in _EXECUTION_TRANSPORTS:
                return tool
        if not compiled and tool.executor.transport not in _EXECUTION_TRANSPORTS:
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
                                ttl_seconds=screening.approval_ttl_seconds,
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

        return _clone_tool(tool, wrap_executor(inner, execute))

    return _wrap_tools(tools, wrap_one)


# Default deny/approval patterns when command_screening.include_defaults is true.
_DEFAULT_COMMAND_RULES: tuple[tuple[str, Literal["allow", "deny", "require_approval"], str], ...] = (
    (r"rm\s+(-[a-zA-Z]*f[a-zA-Z]*\s+)*(/|~|/etc|/var|/usr|/home)", "deny", "destructive rm"),
    (r"\bmkfs\b|\bdd\s+if=", "deny", "disk wipe"),
    (r":\(\)\s*\{\s*:\|:&\s*\}\s*;", "deny", "fork bomb"),
    (r"\bcurl\b.*\|\s*(ba)?sh\b|\bwget\b.*\|\s*(ba)?sh\b", "deny", "pipe remote shell"),
    (r"\bchmod\s+(-R\s+)?777\b", "require_approval", "world-writable chmod"),
    (r"\bsudo\b|\bsu\s", "require_approval", "privilege escalation"),
)


# The marker scan reuses `governance/content_screening.py:_INJECTION` rather than keeping its
# own list. There were two, and this one held the less careful copy: a bare `"system prompt"`
# substring, where the other module already had `system\s+prompt\s*:` and `<\s*/?\s*system\s*>`.
#
# That mattered once `cowork.yaml` enabled screening over its client tools. `"system prompt"`
# matches any *mention* of the phrase, so `cat CLAUDE.md` on this very repository — 23 of its
# files contain it — had its entire output replaced by `[quarantined]`; `replace_tool_output`
# swaps the whole string, it does not redact the match. A control that eats a developer's
# `git log -p` is a control someone turns off, and turning it off would have removed screening
# from `local_shell` too.
#
# Not a sensitivity trade invented here: the anchored patterns still flag
# "ignore previous instructions ..." and "System prompt: you are now ...", and they are the
# ones this repo had already thought about. A second partial copy of a rule is the shape
# `tests/unit/test_invariants.py` was written to catch.

# Trust is an allowlist, not a denylist. `Tool.executor.transport` is an open `str`
# (tools/types.py) — a plugin may mint its own — so an untrusted-denylist silently fails
# *open*: any transport nobody remembered to list skipped content screening entirely. That
# already bit two in-tree transports, "http" (HttpExecutor returns arbitrary remote body
# text) and "client" (content originates in the user's browser). Only transports that
# execute in this process are trusted; everything else is screened by default.
_TRUSTED_TRANSPORTS = frozenset({"local"})

# Defence in depth for the one case the transport check cannot see: a tool that
# claims the in-process transport but is bound to something external. Kept in step
# with `_UNTRUSTED_TRANSPORTS`' former members — a gap here is a trusted tool.
_UNTRUSTED_SOURCE_PREFIXES = (
    "mcp",
    "peer",
    "a2a",
    "queue",
    "browser",
    "client",
    "sandbox",
    "container",
    # A fetched page is attacker-controlled input in the same way a browser page is: the
    # model chose the URL, and whatever answers gets to write into the transcript.
    "http",
    # A search result's title and snippet are written by whoever ranked for the query.
    "search",
    # A retrieved chunk is text somebody ingested. The model chose neither the destination
    # nor the endpoint here — but it did not write the document either, and an agent that
    # quotes a chunk into its answer is relaying it.
    #
    # Redundant today, and listed anyway: `_TRUSTED_TRANSPORTS` is an allowlist of `local`,
    # so the transport check above already catches this and no test can tell this entry from
    # its absence. It earns its place by covering the case that check cannot — a tool whose
    # transport is `local` but whose source is a retrieval binding — which is what the `http`
    # and `search` entries beside it are also for.
    "documents",
    # A recalled memory is a relay, not a source. Capture runs over turns that carried untrusted
    # tool output, so a payload screening quarantined on its way in can be extracted as a "fact"
    # and handed back by `recall` or `list_memories` turns later. Unlike `documents` this entry
    # is not redundant: memory tools are `transport: local`, and without it they were screened
    # only where a manifest named them — `cowork` did, `governed` did not.
    "memory",
)


def _is_untrusted_tool(tool: Tool) -> bool:
    """True unless the tool executes in-process. Unknown transports are untrusted."""
    if tool.executor.transport not in _TRUSTED_TRANSPORTS:
        return True
    source = tool.source or ""
    return source.startswith(_UNTRUSTED_SOURCE_PREFIXES)


def apply_content_screening(
    tools: list[Tool],
    screening: ContentScreening | None,
    manifest_id: str,
    *,
    decider: MeteredDecider | None = None,
    images: Callable[[], ImageScreener] | None = None,
    imported_skills: bool = False,
) -> list[Tool]:
    """Screen what a tool returns before the model reads it -- its text, and any image.

    `images` makes the per-call image screener (`inbound.tool_image_screener`), present when
    `image_model` is set. Without it an *untrusted* tool's images are quarantined: the text
    screeners cannot read pixels, and a manifest that turned screening on to make a browser
    safe must not read as covered while a screenshot carries a payload past it.

    `imported_skills`: the catalog holds a skill built on a GitHub import. With screening off,
    this still installs the free marker scan -- no model, no decider -- on what the relaying
    tools (`relays_untrusted`: `activate_skill`, `read_skill_file`, `list_skills`) return of it,
    and quarantines a match: third-party instructions are not left wholly unread because a
    manifest never turned screening on. Everything else stays as off as the manifest says.
    """
    enabled = screening is not None and screening.enabled
    if not enabled and not imported_skills:
        return tools
    # The relayed-only floor: the markers alone, on relayed results alone, quarantining.
    markers_only = not enabled
    floor = screening if screening is not None and enabled else ContentScreening()
    on_flag = floor.on_flag
    named = list(floor.tools)
    model_id = floor.model.strip()
    scored_only = list(floor.model_tools)

    def wrap_one(tool: Tool) -> Tool:
        # Additive: what `tools` names, *plus* every untrusted tool, always.
        #
        # These used to be alternatives — a non-empty `tools` list replaced the untrusted-tool
        # default rather than adding to it. So the natural way to *extend* screening to one
        # trusted local tool silently turned it off for every `mcp__*`, `peer__*`, browser,
        # sandbox, container and queue tool, while the manifest still read as a working
        # control. Injected content on a fetched page then reached the model with the whole
        # governed toolset behind it.
        #
        # There is no safe narrowing here, which is why the escape hatch is gone rather than
        # renamed: turning screening off for untrusted output is the thing screening exists to
        # prevent. `matches_any([], name)` is False, so a manifest that never sets `tools`
        # behaves exactly as before.
        if markers_only and not tool.relays_untrusted:
            return tool
        untrusted = _is_untrusted_tool(tool) and not markers_only
        text_covered = (matches_any(named, tool.name) or untrusted) and not markers_only
        # An image tool's *results* are screened by where their input came from, its summary
        # text is not: it is a size and a reference the tool wrote itself.
        image_tool = tool.source == "image"
        if not (text_covered or image_tool or tool.relays_untrusted):
            return tool
        inner = tool.executor
        # The paid scoring, by `model_tools`; the markers below run regardless. Never on the
        # relayed-only floor: it is the free scan a manifest gets without asking.
        paid = not markers_only and (not scored_only or matches_any(scored_only, tool.name))

        async def execute(args: ToolInput, ctx: ToolInvocationCtx | None = None) -> ToolOutput:
            out = await inner.execute(args, ctx)
            if is_wrapper_deny(out):
                return out
            # Per call as well as per tool: a trusted tool may relay one result from an untrusted
            # author (`untrusted_output` -- an imported skill's body from `activate_skill`), and
            # that result is screened as an untrusted tool's is.
            relayed = is_untrusted_output(out)
            if not (text_covered or relayed):
                return await screen_images(out) if image_tool else out
            content = tool_output_content(out)
            flagged = any(rx.search(content) for rx in _INJECTION)
            unavailable = False
            if not flagged and paid and (model_id or decider is not None):
                from felix.config import get_settings
                from felix.governance.tool_screening import screen_tool_output

                # Every window of the output, not the first: a benign prefix longer than one
                # screener window used to carry the payload past both the model and the decider.
                result = await screen_tool_output(get_settings(), content, model_id, decider)
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
                return replace_tool_output(
                    out, content="[quarantined] tool output could not be screened", images=[]
                )
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
                # A quarantined output shows the model nothing: its images go with its text.
                return replace_tool_output(out, content=notice, images=[])
            return await screen_images(out)

        async def screen_images(out: ToolOutput) -> ToolOutput:
            if not tool_output_images(out):
                return out
            if images is not None:
                return await _screen_tool_images(out, images(), tool.name)
            if untrusted or is_untrusted_output(out) or _image_from_workspace(out):
                # Fail closed. Text from an untrusted tool is always marker-scanned; its pixels
                # have no screener without `image_model`, and a page can draw its payload rather
                # than write it. An image tool's result from a workspace file is the same case:
                # nothing screened those bytes. A trusted tool named in `tools` keeps its images,
                # and so does an image tool's result from a thread image -- screened, if at all,
                # when it entered the thread, and no less so for being cropped.
                record_counter(
                    "felix_content_screening",
                    {"manifest_id": manifest_id, "tool": tool.name, "action": "image_unscreened"},
                )
                return replace_tool_output(
                    out, content=f"{tool_output_content(out)}\n{TOOL_IMAGE_UNSCREENED}", images=[]
                )
            return out

        return _clone_tool(tool, wrap_executor(inner, execute))

    return _wrap_tools(tools, wrap_one)


async def _close_if_timed_out(req: Any, approval_id: str, note: str) -> None:
    """Write a timeout back to the row, so `/approvals` stops offering a decided call.

    Best effort: the denial has already been returned to the caller, and a store error
    here must not turn a refused tool call into a failed run.
    """
    if note != "timeout" or not approval_id:
        return
    try:
        from felix.approvals import store as approvals_store

        await approvals_store.close_timed_out(req.settings, req.auth.tenant_id, approval_id)
    except Exception:
        logger.debug("approvals store close_timed_out failed", exc_info=True)


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

    # Resolved before the row is written, not after: a durable run's only channel is the row
    # (the frame cannot cross from the worker to the API's stream), so the thread goes on both.
    thread_id = (ctx.thread_id if ctx else None) or req.thread_id
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
            # Same argument as `thread_id` above, for the same reason: the row is a durable
            # run's only channel, so anything the frame says has to be on the row too or the
            # poll shows strictly less than the stream.
            reason=reason,
            thread_id=thread_id or "",
            tool_call_id=(ctx.tool_call_id if ctx else "") or "",
        )
    except Exception:
        logger.debug("approvals store create_pending failed", exc_info=True)
        return False, args, "approvals unavailable"

    approval_id = str(pending_row.get("id") or "")
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
            # The deadline after which the harness stops waiting and denies. Read off the
            # row rather than recomputed, so the frame and the poll cannot disagree about
            # when the offer expires -- and null here means "no rule TTL", which is a real
            # state the client renders from its own default rather than a missing value.
            "expires_at": pending_row.get("expires_at"),
        },
    )
    decision = await wait_for_decision(
        approval_id,
        timeout=float(ttl_seconds) if ttl_seconds else None,
    )
    if decision.decision != "approved":
        await _close_if_timed_out(req, approval_id, decision.note)
        return False, args, decision.note or "denied"
    if decision.edited_args:
        args = dict(decision.edited_args)
    return True, args, ""


def apply_limits(tools: list[Tool], limits: Limits | EffectiveLimits, manifest_id: str) -> list[Tool]:
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

            max_calls = limits.max_tool_calls
            if max_calls is not None and ls.tool_calls >= max_calls:
                return deny_output(
                    f"[limits] max_tool_calls ({max_calls}) exceeded",
                    "limits",
                )
            max_hops = limits.max_peer_hops
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

        return _clone_tool(tool, wrap_executor(inner, execute))

    return _wrap_tools(tools, wrap_one)


def apply_guardrails(tools: list[Tool], guardrails: Guardrails | None, manifest_id: str) -> list[Tool]:
    if guardrails is None:
        return tools
    providers = set(guardrails.providers)
    if "pii" not in providers:
        return tools
    block = guardrails.block_on_match
    targets = set(guardrails.targets)
    # `output` is tool output and the reply; `final_response` is the reply alone, which
    # `apply_reply_controls` handles. Only `output` wraps tools (the default includes it).
    if targets and "output" not in targets:
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
            return replace_tool_output(out, content=result.text)

        return _clone_tool(tool, wrap_executor(inner, execute))

    return _wrap_tools(tools, wrap_one)


def apply_judges(
    tools: list[Tool],
    guardrails: Guardrails | None,
    manifest_id: str,
    *,
    decider: MeteredDecider | None = None,
) -> list[Tool]:
    """Apply tool-output judges (heuristic, or LLM when JudgeRule.model is set)."""
    _ = manifest_id
    judges = [j for j in (guardrails.judges if guardrails else []) if not j.final_response]
    if not judges:
        return tools

    def wrap_one(tool: Tool) -> Tool:
        applicable = [j for j in judges if not j.target_tools or matches_any(j.target_tools, tool.name)]
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
                score = await judge_score(content, j, settings=settings, decider=decider)
                threshold = float(getattr(j, "threshold", 0.7) or 0.7)
                if score < threshold:
                    return deny_output(
                        f"[judge denied] {j.name}: score={score:.2f} < {threshold}",
                        "guardrails",
                    )
            return out

        return _clone_tool(tool, wrap_executor(inner, execute))

    return _wrap_tools(tools, wrap_one)


def apply_reply_controls(
    agent: Agent,
    guardrails: Guardrails | None,
    manifest_id: str,
    *,
    decider: MeteredDecider | None = None,
    screen: ReplyScreen | None = None,
) -> Agent:
    """The reply-path controls: `final_response` judges and PII guardrails on the reply.

    Wraps the agent rather than its tools, because the reply is not a tool output. The
    mechanics live in `felix.governance.reply`; this is the slot in the compile that
    applies them, last, after the pattern has been built. `screen` is the one the
    pattern's session store was given, so the log and the reply share each verdict.
    """
    from felix.governance.reply import ReplyControlsAgent, reply_controls_enabled

    if guardrails is None or not reply_controls_enabled(guardrails):
        return agent
    return ReplyControlsAgent(  # type: ignore[return-value]
        agent, guardrails, manifest_id, decider=decider, screen=screen
    )


def reply_screen_for(
    guardrails: Guardrails | None,
    manifest_id: str,
    *,
    decider: MeteredDecider | None = None,
    parent: ReplyScreen | None = None,
) -> ReplyScreen | None:
    """The reply screen a compile shares between its session writes and its reply."""
    from felix.governance.reply import reply_controls_enabled

    if guardrails is None or not reply_controls_enabled(guardrails):
        return None
    return ReplyScreen(guardrails, manifest_id, decider=decider, parent=parent)


def _arg_present(args: ToolInput, name: str) -> bool:
    """Whether `name` was meaningfully supplied.

    Three separate questions -- is the key there, is it null, is it empty -- and the
    obvious `str(args.get(name) or "").strip()` answers all three with truthiness after
    string coercion. That reads `0`, `0.0` and `False` as absent, so the *same* logical
    value gates or does not gate depending on whether the model emitted it as a JSON
    number or a JSON string. Models are inconsistent about that, and the resulting
    coin-flip resolves toward *no approval* -- the wrong direction for a control.

    Emptiness still counts as absence for `str`, `list`, `dict`, `tuple` and `set`,
    where an empty value genuinely means "not supplied". `put_memory` strips and then
    maps a blank `topic_key` to `None` for the same reason, so the gate and the store
    agree on what a blank key means -- an agreement that has to be maintained on both
    sides, and did not hold until the store learned to strip.

    Everything else is present, including `0`, `0.0` and `False`. That fallthrough is
    deliberate: an unrecognised type is a supplied value, and a control should fail
    toward gating. It also means this function never invokes a user-defined `__bool__`
    -- `in` hashes a string key, `is None` is identity, `isinstance` touches no dunder,
    and `bool()` is only ever called on builtins. A generic `bool(value)` would have
    propagated a `ValueError` out of a governance wrapper for anything array-shaped.
    """
    if name not in (args or {}):
        return False
    value = args[name]
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list | dict | tuple | set):
        return bool(value)
    return True


PREVIEW_ARG = "preview"
# A preview is computed on the approval path, ahead of a person reading it; it must not be the
# thing that holds a run open. Past this the call is refused, exactly as if the preview raised.
_PREVIEW_TIMEOUT_S = 60.0


class _PreviewFailed(Exception):
    """A tool with an `approval_preview` could not produce one. The message is redacted."""


async def _approval_preview(tool: Tool, args: ToolInput) -> str | None:
    """The text a person reads before approving `tool`, or None when the tool has none.

    Raises `_PreviewFailed` when the tool has a preview and it raises or times out. That fails
    the call closed rather than writing a row without one: a tool carries a preview because its
    arguments are a *reference* to the content (`publish_commits` takes a sha, not the files),
    so an approval granted over `preview unavailable` binds content nobody saw — and the model
    can provoke the failure, e.g. by naming a sha that is not a commit yet when the preview runs
    and is one by the time the approval comes back. Known secret values are redacted from the
    preview and from the failure reason alike: the row is read by whoever holds
    `approvals:read`, and the reason goes back to the model.
    """
    fn = tool.approval_preview
    if fn is None:
        return None
    import asyncio

    from felix.secrets import redact_text

    try:
        async with asyncio.timeout(_PREVIEW_TIMEOUT_S):
            text = str(await fn(dict(args)))
    except TimeoutError:
        raise _PreviewFailed(f"timed out after {_PREVIEW_TIMEOUT_S:.0f}s") from None
    except Exception as exc:
        logger.warning("approval preview for %s failed", tool.name, exc_info=True)
        # Redact, then cut: cut first and a secret straddling the cut survives as a prefix.
        raise _PreviewFailed(redact_text(f"{type(exc).__name__}: {exc}")[:200]) from None
    return redact_text(text)


def _without_preview(tool: Tool, args: ToolInput) -> ToolInput:
    """Arguments an approver sent back, minus the preview the harness added for them to read.

    Only for a tool that has a preview: on any other tool `preview` is an ordinary argument name.
    """
    if tool.approval_preview is None:
        return dict(args)
    return {k: v for k, v in args.items() if k != PREVIEW_ARG}


def apply_approvals(tools: list[Tool], rules: list[ApprovalRule], manifest_id: str) -> list[Tool]:
    if not any(r.tools for r in rules):
        return tools
    from felix.manifests.approval_args import warn_unknown_when_args

    # A `when_args` name no gated tool takes is a rule that never fires. Warned, not refused:
    # an MCP tool's schema can change under a stored manifest, and that must not be an outage.
    warn_unknown_when_args(rules, tools, manifest_id)

    def wrap_one(tool: Tool) -> Tool:
        # Approvals is the only control in the stack that selects *one* rule — policies and
        # judges apply every match conjunctively, so for them more matches can only tighten.
        # Here the chosen rule decides `ttl_seconds`, `one_shot`, `bind_principal` and
        # `when_args`, so "which rule matched" is the whole gate.
        #
        # A literal name therefore beats a pattern, and among literals the last still wins —
        # which is exactly what the previous `gated[name] = r` dict did, since a glob rule
        # contributed nothing to it. That makes globbing here provably non-weakening: every
        # tool gated before is gated by the same rule as before, and a pattern can only gate
        # a tool that nothing gated.
        #
        # Plain last-match-wins was not that. With a strict literal rule followed by a broad
        # `github__*` audit rule carrying `when_args: [force]`, the pattern won and every call
        # without `force` ran ungated: a one-shot, principal-bound gate on a destructive tool,
        # removed by adding a rule. That shape is expected in the wild precisely because the
        # docs told operators to write the glob that never worked.
        literal = [r for r in rules if tool.name in r.tools]
        matched = literal or [r for r in rules if matches_any(r.tools, tool.name)]
        rule = matched[-1] if matched else None
        if rule is None:
            return tool
        inner = tool.executor

        async def execute(args: ToolInput, ctx: ToolInvocationCtx | None = None) -> ToolOutput:
            import hashlib
            import json

            from felix.approvals import store as approvals_store
            from felix.approvals.interrupt import wait_for_decision
            from felix.side_events import emit as emit_side_event

            # A rule with `when_args` gates only the calls that carry those arguments.
            # The tool is otherwise untouched -- same executor, no approval, no side
            # event -- so a conditional rule costs nothing on the calls it does not
            # cover.
            if rule.when_args and not all(_arg_present(args, name) for name in rule.when_args):
                return await inner.execute(args, ctx)

            req = try_get_context()
            granted = bool((req.extras if req else {}).get(f"approval:{tool.name}"))
            pending_row: dict[str, object] | None = None
            preview_failure: str | None = None
            # What the row and the frame show. Equal to `args` unless the tool computes a
            # preview, which is added here and nowhere else: the signature below is hashed from
            # `args`, so a preview can neither widen nor narrow what an approval authorizes.
            shown_args: ToolInput = dict(args)
            # Before `create_pending`, so the row carries it too — `GET /approvals` is the only
            # channel a durable run has, and it was the half with no thread on it.
            thread_id = (ctx.thread_id if ctx else None) or (req.thread_id if req else None)
            if not granted and req is not None:
                try:
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
                        # `on_behalf_of` when a machine actor is running A's work — a resumed
                        # durable fiber is principal `fiber`, and without this A's grant would
                        # not match its own run. Empty for every ordinary caller, so this
                        # changes nothing outside that case.
                        principal_subj=(
                            (req.auth.on_behalf_of or req.auth.principal_sub or "")
                            if rule.bind_principal
                            else None
                        ),
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
                            args = _without_preview(tool, approved["edited_args"])
                    else:
                        preview = await _approval_preview(tool, args)
                        if preview is not None:
                            shown_args[PREVIEW_ARG] = preview
                        pending_row = await approvals_store.create_pending(
                            req.settings,
                            req.auth.tenant_id,
                            manifest_id=manifest_id,
                            tool_name=tool.name,
                            call_signature=sig,
                            args=dict(shown_args),
                            principal_subj=req.auth.principal_sub,
                            rule_id=rule.id,
                            ttl_seconds=rule.ttl_seconds,
                            # On the row, not only in the frame. `description` is the one
                            # field in `ApprovalRule` written to be read by a person, and
                            # the poll -- a durable run's only channel -- could not show it.
                            reason=rule.description,
                            thread_id=thread_id or "",
                            tool_call_id=(ctx.tool_call_id if ctx else "") or "",
                        )
                except _PreviewFailed as exc:
                    # No row: an approval over a missing preview binds content nobody saw.
                    preview_failure = str(exc)
                except Exception:
                    logger.debug("approvals store lookup failed", exc_info=True)

            if preview_failure is not None:
                record_counter(
                    "felix_approval_preview_failed",
                    {"manifest_id": manifest_id, "tool": tool.name, "rule": rule.id},
                )
                return deny_output(
                    f"[approval preview failed] tool={tool.name} rule={rule.id}: {preview_failure}. "
                    "No approval was requested; fix the call and retry.",
                    "approvals",
                )

            # `create_pending` shares a live row between identical calls, keyed on the signature
            # alone. Under `bind_principal` that let a second caller join the first caller's
            # request: the approver read the first principal, granted it, and the second
            # caller's waiting call ran on it -- and, with `one_shot`, spent it. A row opened by
            # someone else is not this caller's to wait on.
            if (
                not granted
                and pending_row is not None
                and rule.bind_principal
                and req is not None
                and str(pending_row.get("principal_subj") or "") != str(req.auth.principal_sub or "")
            ):
                record_counter(
                    "felix_approval_required",
                    {"manifest_id": manifest_id, "tool": tool.name, "rule": rule.id},
                )
                return deny_output(
                    f"[approval required] tool={tool.name} rule={rule.id}: an identical request "
                    "from another caller is already pending; this caller's request was not joined to it.",
                    "approvals",
                )

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
                await emit_side_event(
                    thread_id,
                    "approval_required",
                    {
                        "approval_id": approval_id,
                        "tool_name": tool.name,
                        "args": dict(shown_args),
                        "rule_id": rule.id,
                        # The operator's own words for why this gate exists. Without it the
                        # frame named the rule and nothing else, and `description` -- the one
                        # field in `ApprovalRule` written to be read by a person -- reached no
                        # client by any route: the `/approvals` row does not carry it either.
                        # A banner had `workspace-write` to explain itself with.
                        "reason": rule.description,
                        "thread_id": thread_id,
                        "tool_call_id": ctx.tool_call_id if ctx else None,
                        # Off the row, so the frame and the poll cannot disagree about when
                        # the offer expires. The frame carried no deadline at all, which is
                        # why `@felix/client` documents `expiresAt` as poll-only.
                        #
                        # On the *reuse* path this is the first caller's deadline, not this
                        # call's -- `create_pending` matches on (tenant, manifest, tool,
                        # signature) and does not look at `expires_at`, while
                        # `wait_for_decision` below times out on *this* rule's ttl. It fails
                        # closed either way: an approval granted past its stored expiry is
                        # refused by `find_approved`. Same qualifier `thread_id` and
                        # `tool_call_id` carry on the row.
                        "expires_at": pending_row.get("expires_at"),
                    },
                )
                # Every other key here is *this* caller's, read from `rule`/`ctx`, and that
                # asymmetry is load-bearing rather than untidy. Deriving the whole frame from
                # `pending_row` is the obvious tidy-up and would be a cross-thread leak: a
                # reused row holds the ids of whichever call opened it, so thread A's
                # `thread_id` and `tool_call_id` would be emitted into thread B's stream.
                # Only `expires_at` is the row's to give.
                decision = await wait_for_decision(
                    approval_id,
                    timeout=float(rule.ttl_seconds) if rule.ttl_seconds else None,
                )
                if decision.decision != "approved":
                    await _close_if_timed_out(req, approval_id, decision.note)
                    note = decision.note or "denied"
                    return deny_output(
                        f"[approval {note}] tool={tool.name} rule={rule.id}",
                        "approvals",
                    )
                # one_shot spends the grant here too. Only the `find_approved` path consumed it,
                # so the call that waited for the decision ran and left the grant unspent — one
                # replay of the same signature later found it and ran again. Single-winner, so
                # two calls parked on one reused row cannot both run on one decision.
                if rule.one_shot and (
                    req is None
                    or not await approvals_store.consume_approval(
                        req.settings, req.auth.tenant_id, approval_id
                    )
                ):
                    return deny_output(
                        f"[approval already used] tool={tool.name} rule={rule.id}",
                        "approvals",
                    )
                if decision.edited_args:
                    args = _without_preview(tool, decision.edited_args)
                return await inner.execute(args, ctx)
            return await inner.execute(args, ctx)

        return _clone_tool(tool, wrap_executor(inner, execute))

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


def _warn_unmatched_tool_patterns(m: Manifest, bound: list[str]) -> None:
    """Say so when a governance rule targets tools that are not there."""
    targets: list[tuple[str, str, list[str]]] = []
    for policy in m.spec.policies:
        targets.append(("policy", policy.id, list(policy.tools)))
    for rule in m.spec.approvals:
        targets.append(("approval", rule.id, list(rule.tools)))
    if m.spec.guardrails:
        for judge in m.spec.guardrails.judges:
            if judge.final_response:
                # `apply_reply_controls` never reads target_tools, so any value here is
                # ignored. Warning about *unmatched* patterns would be right by accident and
                # silent when they match — say the real thing instead.
                if judge.target_tools:
                    logger.warning(
                        "judge %r sets final_response, so its target_tools are ignored",
                        judge.name,
                        extra={"manifest_id": m.metadata.name},
                    )
                continue
            targets.append(("judge", judge.name, list(judge.target_tools)))
    if m.spec.tool_guidance:
        targets.append(("tool_guidance", "tool_guidance", list(m.spec.tool_guidance)))
    if m.spec.content_screening and m.spec.content_screening.enabled:
        targets.append(("content_screening", "content_screening", list(m.spec.content_screening.tools)))
        paid = list(m.spec.content_screening.model_tools)
        if paid:
            targets.append(("content_screening", "content_screening.model_tools", paid))
    if m.spec.command_screening and m.spec.command_screening.enabled:
        targets.append(
            ("command_screening", "command_screening", list(m.spec.command_screening.target_tools))
        )

    for kind, rule_id, patterns in targets:
        missing = unmatched_patterns(patterns, bound)
        if missing:
            logger.warning(
                "%s %r targets %s, which match no bound tool — it gates nothing",
                kind,
                rule_id,
                ", ".join(repr(x) for x in missing),
                extra={"manifest_id": m.metadata.name},
            )
            record_counter(
                "felix_rule_targets_nothing",
                {"manifest_id": m.metadata.name, "kind": kind, "rule": rule_id},
            )


def _summarise(names: list[str], limit: int = 8) -> str:
    """Name a few and say how many more, so a truncated list does not read as complete."""
    shown = ", ".join(repr(n) for n in names[:limit])
    extra = len(names) - limit
    return f"{shown} and {extra} more" if extra > 0 else shown


def _warn_max_turns_does_not_bound_this_loop(m: Manifest) -> None:
    """A single-agent manifest that sets `max_turns` and not `recursion_limit` bounded nothing.

    `max_turns` is read by the delegating patterns; a `react` loop is bounded by
    `recursion_limit`. Three bundled manifests carried `max_turns: 40` on a react agent, and
    the first live run stopped at the default ten steps. A warning and the inert-rule counter
    rather than a refusal: a stored manifest with the field must keep compiling.
    """
    from felix.patterns.registry import is_multi_agent_pattern

    if is_multi_agent_pattern(m.spec.pattern):
        return
    if "max_turns" in m.spec.model_fields_set and m.spec.recursion_limit is None:
        logger.warning(
            "spec.max_turns is set on a %r agent, which is bounded by spec.recursion_limit — "
            "max_turns bounds nothing here (manifest=%s)",
            m.spec.pattern,
            m.metadata.name,
        )
        record_counter(
            "felix_rule_targets_nothing",
            {"manifest_id": m.metadata.name, "kind": "max_turns", "rule": "max_turns"},
        )


TOOL_IMAGE_UNSCREENED = (
    "[quarantined] image from an untrusted tool not shown: set content_screening.image_model to screen it"
)


def _image_from_workspace(out: ToolOutput) -> bool:
    from felix.tools.image_tools import IMAGE_INPUT_KEY

    return (output_metadata(out) or {}).get(IMAGE_INPUT_KEY) == "workspace"


def _warn_screenshots_are_quarantined(m: Manifest) -> None:
    """Say so at compile time when a browser screenshot can never reach the model.

    Under content screening without `image_model`, an untrusted tool's images are quarantined
    (`apply_content_screening`). A manifest binding `op: screenshot` then has a tool that always
    returns a note instead of a picture -- a configuration that works, and does nothing.
    """
    screening = m.spec.content_screening
    if not screening.enabled or screening.image_model.strip():
        return
    shots = sorted(ref.name for ref in m.spec.browser_tools if ref.op == "screenshot")
    if shots:
        logger.warning(
            "manifest %r binds screenshot tool(s) %s under content_screening without image_model, "
            "so every screenshot is quarantined; set content_screening.image_model to screen them",
            m.metadata.name,
            _summarise(shots),
            extra={"manifest_id": m.metadata.name},
        )


def _warn_untrusted_tools_are_unscreened(m: Manifest, untrusted: list[str]) -> None:
    """Say so when untrusted tool output reaches the model with nothing looking at it.

    `content_screening.enabled` defaults to False, and `validate_governance` requires it only under
    `eu_ai_act` — `soc2` does not, and its data-governance check is satisfiable by guardrails
    instead. So a manifest that binds an MCP server, an
    A2A peer, a browser, a sandbox, a container or a queue and never enables screening is a
    normal, valid manifest in which attacker-controlled text reaches the model with the whole
    governed toolset behind it — the last remaining path of that shape after the additive-
    screening change.

    A warning, not a default flip and not a refusal. Turning screening on by default would
    change the cost and the behaviour of every existing deployment binding an MCP server,
    which is not a thing to do silently in a patch; refusing would break them outright. What
    was missing is anything saying it at the moment the manifest is compiled.

    Silent across every bundled manifest, which is the bar for shipping it — a warning that
    fires on what we ship is noise on arrival. `contributor.yaml` and `cowork.yaml` are the
    two that bind untrusted tools, and both enable screening.
    """
    if not untrusted:
        return
    # `content_screening` has a default_factory, so it is never None — only disabled.
    if m.spec.content_screening.enabled:
        return
    logger.warning(
        "manifest %r binds untrusted tool(s) %s with content_screening disabled, so their "
        "output reaches the model unscreened",
        m.metadata.name,
        _summarise(sorted(untrusted)),
        extra={"manifest_id": m.metadata.name},
    )
    record_counter("felix_untrusted_tools_unscreened", {"manifest_id": m.metadata.name})


def _warn_imported_skills_are_unscreened(m: Manifest, catalog: Any) -> None:
    """Say so when a skill built on an import is in the catalog and only the marker floor screens it.

    What `activate_skill`, `read_skill_file` and `list_skills` return of such a skill is marked as
    relayed from an untrusted author. With content screening off, only the free marker scan reads
    that mark (`apply_content_screening(imported_skills=True)`): no scoring model, no decider.
    The same warning, for the same reason, as `_warn_untrusted_tools_are_unscreened`. Keyed on
    the catalog rather than on the skill tools being bound: those are bound on every manifest
    with skills, and a warning that fires on every bundled manifest is noise.
    """
    if m.spec.content_screening.enabled:
        return
    imported = sorted(s.name for s in catalog.skills.values() if s.untrusted)
    if not imported:
        return
    logger.warning(
        "manifest %r offers imported skill(s) %s with content_screening disabled, so what the skill "
        "tools return of them is checked only by the injection markers",
        m.metadata.name,
        _summarise(imported),
        extra={"manifest_id": m.metadata.name},
    )
    record_counter("felix_imported_skills_unscreened", {"manifest_id": m.metadata.name})


def bind_decider(spec: DeciderSpec, settings: Any) -> MeteredDecider | None:
    """`spec.decider`, built once per compile, or None when the manifest names none.

    An id missing from `FELIX_DECISION_ROUTES` fails the compile, the way an unknown
    `spec.model.id` does. Consumers fall back when a *call* fails; a decider that could never
    have been reached is a configuration mistake, and falling back from it silently would
    leave a manifest believing it had a decider it has never used.
    """
    if not spec.id:
        return None
    from felix.config import get_settings
    from felix.decisions import build_decider

    return build_decider(settings or get_settings(), spec.id, min_confidence=spec.min_confidence)


def _warn_policies_cannot_be_satisfied(m: Manifest, settings: Any) -> None:
    """Say so at compile when nothing in this configuration can hold a scope.

    `apply_policies` denies when a required scope is absent, and it is right to: "no scopes"
    must not read as "all scopes". But several contexts carry an empty scope set by
    construction, so `spec.policies` plus one of them denies *every* policied tool — safe, and
    baffling if you have not read `deploy/GOVERNANCE.md`.

    A warning rather than a refusal. The combination is legitimate — a manifest can be served
    over HTTP to a scoped caller *and* resumed as a fiber — so refusing it would break a
    working deployment to prevent a surprise. Naming it at compile is what turns "my calculator
    stopped working" into a one-line answer.
    """
    if not m.spec.policies:
        return
    reasons: list[str] = []
    if m.spec.execution.mode == "durable":
        reasons.append("execution.mode: durable — a resumed fiber runs as principal 'fiber' with no scopes")
    if str(getattr(settings, "auth_mode", "")) == "none":
        reasons.append("FELIX_AUTH_MODE=none — every caller is anonymous with no scopes")
    if not reasons:
        return
    logger.warning(
        "manifest %r declares policies that nothing in this configuration can satisfy, so every "
        "policied tool will deny: %s",
        m.metadata.name,
        "; ".join(reasons),
        extra={"manifest_id": m.metadata.name},
    )
    record_counter("felix_policy_unsatisfiable", {"manifest_id": m.metadata.name})


def _bind_skill_authoring(
    resolved: list[Tool], m: Manifest, deps: BuildDeps, tenant_id: str, catalog: SkillCatalog
) -> None:
    """`create_skill` / `update_skill` / `submit_skill_feedback`, bound before the governance
    block like every tool, so an approvals rule on them holds the call until a person has read
    what the harness renders as its preview. Feedback is restricted to the library skills in
    ``catalog`` -- the ones this agent was actually given."""
    from felix.skills.authoring import make_skill_authoring_tools, make_skill_feedback_tool

    spec = m.spec.skill_authoring
    _append_unique_tools(
        resolved,
        [
            *make_skill_authoring_tools(
                deps.settings,
                tenant_id=tenant_id,
                manifest_id=m.metadata.name,
                mode=spec.mode,
                max_pending=spec.max_pending,
                object_store=deps.object_store,
                auto_eval=spec.auto_eval,
            ),
            make_skill_feedback_tool(
                deps.settings,
                tenant_id=tenant_id,
                manifest_id=m.metadata.name,
                catalog=catalog,
                max_pending=spec.max_pending,
            ),
        ],
    )


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

        # Bound once, before the sub-agents: the skill suggester, the judges, the reply
        # controls and the pattern share one metered decider, and the reply screen it feeds
        # wraps the session store every agent in this tree writes through.
        decider = bind_decider(m.spec.decider, deps.settings)
        # The pattern writes the session log as the run goes, before the reply wrapper sees
        # the output, so the log is screened at the write with the verdicts the reply gets.
        reply_screen = reply_screen_for(
            m.spec.guardrails, m.metadata.name, decider=decider, parent=deps.reply_screen
        )
        session_store = screen_session_store(deps.session_store, reply_screen)
        # What a pattern screens text leaving by other doors with (memory capture, reflect's
        # quoted draft): this compile's controls and every enclosing compile's. A child with
        # none of its own still owes its router's.
        screen_chain = reply_screen or deps.reply_screen
        # Every render of history screens its images, whoever renders it. Wrapping what the
        # enclosing compile already wrapped is what makes a router's screen hold for the
        # children it forwards the thread to: their own screen adds to it, never replaces it.
        session_strategy = screen_session_strategy(deps.session_strategy, replay_screener(m, deps.settings))

        sub_agents: dict[str, Agent] = {}
        if m.spec.sub_agents:
            # A sub-agent inherits this compile's session store, so its own
            # `spec.memory.checkpointer` is not consulted. Most composites invoke a child
            # with `thread_id=None`, and each session guard in the react loop needs a thread
            # as well as a store — but the router forwards the caller's turn, thread and
            # all, so its child writes the caller's log. That is why children compile
            # against the *screened* store: a router's reply controls would otherwise redact
            # the wire while an unguarded child logged the raw reply.
            builder = deps.sub_agent_builder or _bundled_sub_agent(deps)
            with _compiling_children(
                deps,
                m.metadata.name,
                session_store=session_store,
                reply_screen=screen_chain,
                session_strategy=session_strategy,
            ):
                for name in m.spec.sub_agents:
                    if name not in deps.compiled:
                        deps.compiled[name] = await builder(name)
                    sub_agents[name] = deps.compiled[name]

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
        from felix.manifests.governance import (
            apply_transparency_notice,
            assert_cost_limit_is_measurable,
            validate_governance,
        )
        from felix.manifests.secret_refs import resolve_outbound_secrets

        validate_governance(m, deps.settings)
        assert_cost_limit_is_measurable(m, deps.settings)
        if m.spec.governance.transparency_notice:
            system_prompt = apply_transparency_notice(system_prompt or "", m.metadata.name)

        mcp_refs, peer_refs, container_refs = await resolve_outbound_secrets(m, deps.settings)

        # Outbound MCP servers → remote tools.
        if mcp_refs:
            try:
                from felix.mcp.client import tools_from_mcp_servers

                mcp_tools = await tools_from_mcp_servers(
                    mcp_refs, allow_http=allow_http, manifest_id=m.metadata.name
                )
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

        # Pillow image tools (optional extra). Bound even without Pillow installed: each one
        # then answers that the extra is missing, which says more than a tool that is absent.
        if m.spec.image_tools:
            try:
                from felix.tools.image_tools import tools_from_image_refs

                _append_unique_tools(resolved, tools_from_image_refs(list(m.spec.image_tools)))
            except Exception:
                logger.warning("image tool binding failed", exc_info=True)

        # Fetch tools: the model names the URL, the egress guard pins the address.
        if m.spec.http_tools:
            try:
                from felix.tools.http_fetch import tools_from_http_fetch_refs

                _append_unique_tools(
                    resolved,
                    tools_from_http_fetch_refs(list(m.spec.http_tools), allow_http=allow_http),
                )
            except Exception:
                logger.warning("http fetch tool binding failed", exc_info=True)

        # Web search: the model supplies a query, the operator supplies the endpoint.
        if m.spec.search_tools:
            try:
                from felix.search import build_search_backend
                from felix.tools.web_search import tools_from_search_refs

                _append_unique_tools(
                    resolved,
                    tools_from_search_refs(
                        list(m.spec.search_tools),
                        # `deps.settings` is the reconciled one; `None` resolves to the
                        # null backend, so a compile with no settings binds a tool that
                        # reports it is unconfigured rather than raising here.
                        backend=build_search_backend(deps.settings),
                    ),
                )
            except Exception:
                logger.warning("search tool binding failed", exc_info=True)

        # Publish local commits to GitHub from this process. Bound here, ahead of the governance
        # stack, so approvals — with the diff preview the tool computes — gate it like any write.
        if m.spec.github_publish is not None:
            try:
                from felix.manifests.schema import PERSON_AUTH

                if m.spec.github_publish.auth == PERSON_AUTH:
                    from felix.tools.github_publish import tool_from_thread_publish

                    # Nothing to resolve now: the repository and the token are the thread's,
                    # looked up on each call (`_ThreadPublishExecutor`).
                    _append_unique_tools(
                        resolved, [tool_from_thread_publish(m.spec.github_publish, allow_http=allow_http)]
                    )
                else:
                    if deps.settings is None:
                        raise ValueError("no settings to resolve github_publish.auth with")
                    from felix.secrets import build_secrets, resolve_secret_value
                    from felix.tools.github_publish import tool_from_github_publish

                    token = await resolve_secret_value(
                        build_secrets(deps.settings), m.spec.github_publish.auth
                    )
                    _append_unique_tools(
                        resolved,
                        [tool_from_github_publish(m.spec.github_publish, token=token, allow_http=allow_http)],
                    )
            except Exception:
                # The message names the secret, never its value: `resolve_secret_value` raises
                # `secret not found: NAME`, and the binder raises before a token exists.
                logger.warning("github publish tool binding failed", exc_info=True)

        # Retrieval over the operator's own corpus. Unlike the two above it reaches nothing
        # outbound, so there is no egress to guard — but it needs the tenant, because the
        # corpus is per-tenant and resolving it from anywhere else would read another's.
        if m.spec.document_tools and deps.settings is not None:
            try:
                from felix.tools.document_search import tools_from_document_refs

                _append_unique_tools(
                    resolved,
                    tools_from_document_refs(
                        list(m.spec.document_tools),
                        settings=deps.settings,
                        tenant_id=tenant_id,
                    ),
                )
            except Exception:
                logger.warning("document search tool binding failed", exc_info=True)

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

                _append_unique_tools(
                    resolved, tools_from_sandboxes(list(m.spec.sandboxes), settings=deps.settings)
                )
            except Exception:
                logger.warning("sandbox tool binding failed", exc_info=True)

        # Allowlisted argv on the API host, in the workspace checkout.
        if m.spec.shell_tools:
            try:
                from felix.tools.shell import tools_from_shell_refs

                _append_unique_tools(
                    resolved, tools_from_shell_refs(list(m.spec.shell_tools), settings=deps.settings)
                )
            except Exception:
                logger.warning("shell tool binding failed", exc_info=True)

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

        # Memory tools. Bound here, before the governance block below, so a recalled
        # memory passes through content screening like any other tool output — the
        # automatic fact prelude bypasses the stack entirely.
        if m.spec.memory.recall.tools and m.spec.memory.store != "none" and deps.settings is not None:
            try:
                from felix.memory.tools import make_memory_tools

                _append_unique_tools(
                    resolved,
                    make_memory_tools(
                        settings=deps.settings,
                        tenant_id=tenant_id,
                        manifest_id=m.metadata.name,
                        default_limit=m.spec.memory.recall.limit,
                    ),
                )
            except Exception:
                logger.warning("memory tool binding failed", exc_info=True)

        # The reader for what `spec.artifacts` spills, bound before the governance block below.
        _bind_artifact_reader(resolved, m, deps, tenant_id)

        skill_suggester = None
        # Whether the catalog offers a skill built on an import (`apply_content_screening`).
        imported_skills = False

        # Wire Agent Skills (progressive disclosure + bound skill tools).
        from felix.skills import (
            SKILL_TOOL_NAMES,
            get_skill_activation_store,
            load_manifest_skills,
            make_skill_tools,
            skill_catalog_xml,
        )

        authoring = m.spec.skill_authoring.enabled and deps.settings is not None
        wants_skills = bool(m.spec.skills) or authoring or any(t.name in SKILL_TOOL_NAMES for t in resolved)
        if wants_skills:
            catalog = await load_manifest_skills(
                list(m.spec.skills),
                tenant_id=tenant_id,
                object_store=deps.object_store,
                declared_only=m.spec.skills_declared_only,
                settings=deps.settings,
            )
            skill_tools = {
                t.name: t
                for t in make_skill_tools(
                    catalog,
                    activation_store=get_skill_activation_store(deps.settings),
                    tenant_id=tenant_id,
                    manifest_id=m.metadata.name,
                    settings=deps.settings,
                    object_store=deps.object_store,
                )
            }
            resolved = [skill_tools.get(t.name, t) for t in resolved]
            # Ensure skill tools exist when skills are declared (or authored) but not listed;
            # `read_skill_file` rides along with any of them, since `activate_skill` names files
            # only it can read.
            have = {t.name for t in resolved}
            for name, tool in skill_tools.items():
                if name not in have and (m.spec.skills or authoring or name == "read_skill_file"):
                    resolved.append(tool)
            _warn_imported_skills_are_unscreened(m, catalog)
            imported_skills = any(s.untrusted for s in catalog.skills.values())
            if authoring:
                _bind_skill_authoring(resolved, m, deps, tenant_id, catalog)
            if m.spec.skill_suggestion.enabled and decider is not None and catalog.list_public():
                from felix.skills.suggest import SkillSuggester

                skill_suggester = SkillSuggester(catalog.list_public(), decider, m.spec.skill_suggestion)
            catalog_block = skill_catalog_xml(catalog)
            if catalog_block:
                system_prompt = (
                    f"{system_prompt}\n\n---\n\n{catalog_block}" if system_prompt else catalog_block
                )

        if m.spec.skill_suggestion.enabled and skill_suggester is None:
            # Skills can come from the host rather than `spec.skills`, so this cannot be refused
            # at validation — but an agent with none has nothing to suggest, and says so.
            logger.warning("skill_suggestion is on but %s has no skills to suggest", m.metadata.name)
            record_counter(
                "felix_rule_targets_nothing",
                {"manifest_id": m.metadata.name, "rule": "skill_suggestion", "kind": "skills"},
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
                # 2. Trust. This is model-extracted from earlier turns and can carry
                #    text that originated in tool output. All of it is fenced as
                #    reference material, and none of it is meant to be followed — an
                #    attempt to give user-stated rules an obeyable tier was withdrawn
                #    because the provenance behind it could not be established.
                context_prelude = await active_facts_prompt(
                    deps.settings,
                    tenant_id,
                    manifest_id=m.metadata.name,
                )
            except Exception:
                logger.debug("active facts inject failed", exc_info=True)

        # A pattern that matches no bound tool gates nothing — a typo, a renamed MCP server,
        # or a glob written before its target existed. Logged rather than refused: the bound
        # set legitimately varies (an MCP server whose discovery failed binds no tools), so
        # refusing would let a remote outage take the agent down with it. Silent was not an
        # option either — an inert control that validates is the defect this repo keeps
        # shipping, and globs make one easier to write by hand.
        _warn_unmatched_tool_patterns(m, [t.name for t in resolved])
        _warn_policies_cannot_be_satisfied(m, deps.settings)
        _warn_max_turns_does_not_bound_this_loop(m)
        _warn_untrusted_tools_are_unscreened(m, [t.name for t in resolved if _is_untrusted_tool(t)])
        _warn_screenshots_are_quarantined(m)

        # Governance pipeline (order matters — matches TS builder).
        resolved = apply_secret_masking(resolved, _collect_secrets(deps), m.metadata.name)
        if m.spec.policies:
            resolved = apply_policies(resolved, m.spec.policies, m.metadata.name)
        if m.spec.command_screening.enabled:
            resolved = apply_command_screening(resolved, m.spec.command_screening, m.metadata.name)
        if m.spec.content_screening.enabled or imported_skills:
            # One slot for both: with screening off and an imported skill in the catalog, the same
            # wrapper installs only the free marker scan over what the skill tools relay of it.
            resolved = apply_content_screening(
                resolved,
                m.spec.content_screening,
                m.metadata.name,
                decider=decider if m.spec.content_screening.decider else None,
                images=tool_image_screener(m, deps.settings) if m.spec.content_screening.enabled else None,
                imported_skills=imported_skills,
            )
        # Always installed. Previously gated on any_limit(), so a manifest that declared
        # no limits got no tool-call cap, no wall clock, no token or spend ceiling —
        # and the wrapper silently did nothing when there was no request context.
        resolved = apply_limits(resolved, effective_limits(m.spec.limits), m.metadata.name)
        if guardrails_enabled(m.spec.guardrails):
            resolved = apply_guardrails(resolved, m.spec.guardrails, m.metadata.name)
        if judges_enabled(m.spec.guardrails):
            resolved = apply_judges(resolved, m.spec.guardrails, m.metadata.name, decider=decider)
        if m.spec.approvals:
            resolved = apply_approvals(resolved, m.spec.approvals, m.metadata.name)

        if m.spec.artifacts.enabled:
            from felix.artifacts import apply_artifact_spill
            from felix.config import get_settings

            resolved = apply_artifact_spill(
                resolved,
                m.spec.artifacts,
                object_store=deps.object_store,
                tenant_id=tenant_id,
                manifest_id=m.metadata.name,
                # Never None: the ledger lives here, and a spill without a row is bytes the
                # retention sweep can never find.
                settings=deps.settings if deps.settings is not None else get_settings(),
            )

        final_prompt = (
            system_prompt or f"You are {m.metadata.name}. Use your tools when needed to answer accurately."
        )
        if m.spec.system_prompt.include_tool_guidance:
            guidance = tool_guidance_section(resolved, m.spec.tool_guidance)
            if guidance:
                final_prompt = f"{final_prompt}\n\n{guidance}"

        pattern_builder = get_pattern(m.spec.pattern)
        if pattern_builder is None:
            raise ValueError(
                f"Unknown pattern '{m.spec.pattern}' for manifest '{m.metadata.name}' — "
                f"registered: {', '.join(list_patterns()) or '(none)'}"
            )
        if m.spec.output_schema is not None and not honours_output_schema(m.spec.pattern):
            # Refused rather than dropped. Every pattern receives `output_schema` in its build
            # context and only some read it, so the alternative is a manifest that declares an
            # answer contract, compiles, runs, and returns free text — the defect shape this
            # repo produces most. Checked here rather than in the manifest schema because the
            # pattern registry is open: only the live registry knows what a plugin's pattern
            # supports.
            raise ValueError(
                f"Pattern '{m.spec.pattern}' does not support spec.output_schema "
                f"(manifest '{m.metadata.name}'). Patterns that do: "
                f"{', '.join(sorted(p for p in list_patterns() if honours_output_schema(p)))}"
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
                # Plugin-owned config, namespaced by plugin name. Core never reads
                # inside it; a registered pattern picks out its own key.
                "extensions": dict(m.spec.extensions),
                "recursion_limit": m.spec.recursion_limit,
                "output_schema": m.spec.output_schema,
                "max_turns": m.spec.max_turns,
                "aggregator_prompt": m.spec.aggregator_prompt,
                "session_store": session_store,
                # For model output leaving by a door other than the reply and the log:
                # reflect's quoted draft, and the reply memory capture extracts from.
                "reply_screen": screen_chain,
                "session_strategy": session_strategy,
                "session_spec": m.spec.session,
                "execution": m.spec.execution,
                "limits": effective_limits(m.spec.limits),
                "settings": deps.settings,
                "tenant_id": tenant_id,
                "memory_capture": m.spec.memory.capture,
                "tools_retrieval": m.spec.tools_retrieval,
                "decider": decider,
                "skill_suggester": skill_suggester,
                "procedural_memory": m.spec.procedural_memory,
            }
        )
        from felix.governance.inbound import apply_inbound_controls

        # Outermost: the user turn is screened before anything else sees it, and the
        # reply is screened last. Wrapping here rather than at each entrypoint is what
        # makes "every path a turn takes" true without a list of paths.
        return apply_inbound_controls(
            apply_reply_controls(
                agent, m.spec.guardrails, m.metadata.name, decider=decider, screen=reply_screen
            ),
            m,
            settings,
        )
    finally:
        span.end()


__all__ = ["BuildDeps", "apply_reply_controls", "build_agent"]
