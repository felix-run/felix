---
name: manifest-authoring
description: Author, extend, and debug felix/v1 agent manifests and the schema-to-builder wiring behind them — patterns, tools, skills, session strategies, memory, governance blocks, MCP/A2A/sandbox/queue integrations, and durable execution. Use when writing or editing anything under manifests/, adding a field to the manifest schema, or investigating why a manifest field appears to have no effect.
compatibility: Requires the Felix repo checkout with uv and the felix CLI available.
allowed-tools: Bash(uv run felix:*) Read Grep Glob
---

# Authoring felix/v1 manifests

A manifest is compiled into a governed `Agent` by `packages/harness/src/felix/manifests/builder.py`
at request time. **A field in `manifests/schema.py` does nothing until `builder.py` reads it** —
that is the cause of most "the field is there but the behavior isn't" reports.

## Skeleton

```yaml
# yaml-language-server: $schema=../schemas/manifest.schema.json
apiVersion: felix/v1
kind: Agent
metadata:
  name: my-agent        # must match the file stem — the loader resolves by name
  version: 1.0.0
  description: One sentence on what this agent is for.
  tags: [react]
spec:
  pattern: react        # must be registered in patterns/registry.py
  model:
    temperature: 0
  system_prompt:
    inline: |
      You are Felix …
  tools: [calculator, list_skills, activate_skill, deactivate_skill]
  session:
    strategy: compacting
    reserve_tokens: 16384
    keep_recent_tokens: 20000
    context_window_tokens: 128000
  memory:
    checkpointer: postgres
    store: pgvector
```

Copy `manifests/governed.yaml` when the agent needs governance — it is the fullest worked example
(policies, limits, approvals, screening, guardrails, anomaly, framework mapping, compile pinning).

## Field → code map

| Spec field | Consumed by |
|---|---|
| `pattern` | `patterns/registry.py:get_pattern` → e.g. `patterns/react.py` |
| `tools` | `ToolProvider.resolve` (`tools/provider.py`, builtins in `tools/builtins.py`) |
| `mcp_servers` | `mcp/client.py:tools_from_mcp_servers` → `server__tool` |
| `peers` | `a2a/peers.py:tools_from_peers` → `peer__name` |
| `browser_tools` / `sandboxes` / `containers` / `queues` / `client_tools` | `tools/{browser,sandboxes,queues,client_bridge}.py` |
| `skills` | `felix/skills/` — catalog XML appended to the system prompt |
| `session` | `session/strategies.py` via `runtime.py:build_tenant_agent` |
| `memory` / `procedural_memory` | `memory/{capture,store,procedural}.py` |
| `policies`, `command_screening`, `content_screening`, `limits`, `guardrails`, `approvals`, `artifacts` | the `apply_*` wrappers in `builder.py` (fixed order — see the governance-pipeline skill) |
| `governance` | `manifests/governance.py:validate_governance` (compile-time) |
| `auth.inbound` | `manifests/inbound_auth.py:enforce_inbound_auth` |
| `execution.mode: durable` | `durability/fibers.py` — `/chat` returns `202` + `resume_token` |

See [references/spec-fields.md](references/spec-fields.md) for the per-block details and gotchas.

## Adding a new spec field

1. Add it to the model in `manifests/schema.py` with a sane default (absent field must not change
   behavior).
2. Consume it in `builder.py` — a binder (before the wrapper stack) or an `apply_*` wrapper (in the
   correct slot of the stack).
3. Exercise it in a bundled manifest and add a case to `tests/unit/test_manifest_schema.py` (shape)
   and `tests/unit/test_manifest_governance.py` (behavior, if it is a control).
4. Document it: `guide/manifest-reference.mdx` in the felix-web docs repo (see the docs-sync skill).

## Validate — always

```bash
uv run felix validate-manifest manifests/<name>.yaml -e development
uv run felix validate-manifest manifests/<name>.yaml -e production   # governance-bearing
uv run felix bundle-manifests                                        # all bundled manifests still load
```

CI runs `bundle-manifests` before pytest, so a broken manifest fails the whole build.

**What validation does not catch:** `validate-manifest` checks the schema and the governance
frameworks only. `spec.pattern: nope` validates "ok" and fails later at compile time with
`Unknown pattern`; an unreachable MCP/peer URL binds zero tools with only a logged warning. Smoke
the manifest against a running API (`POST /chat` with `"manifest": "<name>"`) before calling it done.
