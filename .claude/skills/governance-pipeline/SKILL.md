---
name: governance-pipeline
description: How Felix compiles a manifest into a governed agent and how the tool wrapper stack works — secret masking, policies, command and content screening, limits, guardrails, judges, approvals, artifact spill — including how to add a new control in the right slot. Use when editing manifests/builder.py, adding or debugging a governance control, tracing why a tool call was blocked, masked, or paused for approval, or reviewing security-relevant agent behavior.
allowed-tools: Read Grep Glob Bash(uv run:*)
---

# The governance pipeline

`packages/harness/src/felix/manifests/builder.py:build_agent` is the center of Felix. Everything a
manifest declares becomes either a bound tool, a prompt fragment, or a wrapper around a tool.

## Compile order

1. **Resolve** — manifest (DB store → bundled YAML), system prompt, sub-agents, base tools from the
   `ToolProvider`.
2. **Governance compile checks** — `validate_governance()` (frameworks, plaintext-secret policy),
   transparency notice prepended to the prompt, `resolve_outbound_secrets()` for MCP/peer/container
   credentials.
3. **Bind outbound tools** — MCP (`server__tool`), A2A peers (`peer__name`), browser, client
   tools, sandboxes, containers, queues, the procedural-memory writer. Each binder is wrapped in
   `try/except` that logs and continues, so a bad URL yields fewer tools rather than a failure.
4. **Skills + memory** — skill catalog XML and active durable facts appended to the system prompt.
5. **The wrapper stack** — applied to the resolved tool list in this exact order:

   ```
   secret masking
     → policies
       → command screening
         → content screening
           → limits
             → guardrails (PII)
               → judges
                 → approvals
                   → artifact spill
   ```

   The comment `order matters` in the source is load-bearing. Each `apply_*` clones every tool with
   a new executor (`_clone_tool`), so the order determines which control sees the call first and
   which sees the other's output. Later wrappers are *outermost* — approvals gate before limits
   count, masking is innermost and therefore applies to whatever the tool actually returned.
6. **Pattern build** — `get_pattern(spec.pattern)` receives a `PatternBuildContext` dict with the
   wrapped tools, prompt, session store/strategy, limits, memory, and settings.
7. **Final-response judges** — when enabled, `wrap_final_response_judges` wraps the `Agent` itself,
   not its tools.

## Adding a control

1. Add the manifest block in `manifests/schema.py` (default = off, absent = no behavior change).
2. Write `apply_<control>(tools, spec, manifest_id) -> list[Tool]` next to its peers in
   `builder.py`. Follow the existing shape exactly: an inner `wrap_one(tool)` that builds an
   `async def execute(args, ctx=None)`, then `_clone_tool(tool, execute)`.
3. Insert the call into the stack **in the slot that matches its semantics**, not at the end:
   - Sees raw arguments before anything rewrites them → early (policies, command screening).
   - Sees tool output → after the call, later in the stack (content screening, guardrails, judges).
   - Blocks execution pending a human → approvals, late so cheaper checks reject first.
   - Rewrites output for storage → artifact spill, last.
4. Emit an audit event (`audit/emit.py`) when the control fires — a control with no audit trail is
   invisible to `/audit` and to `deploy/GOVERNANCE.md` claims.
5. Test in `tests/unit/test_manifest_governance.py` (fires when configured) and
   `tests/unit/test_deferred_governance.py` (deferred/approval paths). Add a negative test proving
   the control is off by default.

## Debugging "why was this blocked/masked/paused"

- Reproduce with the smallest manifest that has only the suspect block enabled.
- The wrapper that fired names itself in the tool error/output; grep that string in `builder.py`.
- `GET /audit` shows the emitted governance events for the run.
- Untrusted-source screening only applies to tools `_is_untrusted_tool()` marks (MCP, peers,
  browser, queues, sandboxes) — a built-in tool is not screened, by design.
- Approvals surface as an interrupt (`approvals/interrupt.py`); an unattended durable run with
  `allow_unattended: false` will wait for the TTL and then fail — that is expected.

## Related

- `manifests/governance.py` — framework validation and the transparency notice.
- `deploy/GOVERNANCE.md` — the operator-facing SOC2 / EU AI Act control mapping. Update it whenever
  a control's behavior changes.
- `manifests/governed.yaml` — the reference manifest exercising the whole stack.
