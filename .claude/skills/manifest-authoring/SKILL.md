---
name: manifest-authoring
description: Author, extend, and debug felix/v1 agent manifests and the schema-to-builder wiring behind them — patterns, tools, skills, session strategies, memory, governance blocks, MCP/A2A/sandbox/queue integrations, and durable execution. Use when writing or editing anything under manifests/, adding a field to the manifest schema, or investigating why a manifest field appears to have no effect.
compatibility: Requires the Felix repo checkout with uv and the felix CLI available.
allowed-tools: Bash(uv run felix:*) Read Grep Glob
metadata:
  covers: felix/manifests/, felix/patterns/, felix/session/, felix/memory/, felix/plans/, felix/prompts/
---

# Authoring felix/v1 manifests

A manifest is compiled into a governed `Agent` by `packages/harness/src/felix/manifests/builder.py`
at request time. **A field in `manifests/schema.py` does nothing until `builder.py` reads it** —
that is the cause of most "the field is there but the behavior isn't" reports.

The `$schema` header below resolves to `schemas/manifest.schema.json`, which is **generated** from
those pydantic models — run `make schema` after any `manifests/schema.py` change, or
`tests/unit/test_invariants.py` fails and editors validate against a stale schema.

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
    # context_window_tokens: omit it — unset means the model's own window from the catalog
  memory:
    checkpointer: postgres   # postgres | none, or one you register
    store: pgvector
```

`checkpointer: none` runs the agent with no session state — every turn starts from
the messages it was given. It is refused alongside anything the loop would
drop for want of a store: a `session.strategy` other than `full_replay`,
`session.compact_after_turn`, or `memory.capture.enabled`.

Copy `manifests/governed.yaml` when the agent needs governance — it is the fullest worked example
(policies, limits, approvals, screening, guardrails, anomaly, framework mapping, compile pinning).

## Field → code map

<!-- toolkit:enum spec-fields -->
| Spec field | Consumed by |
|---|---|
| `pattern` | `patterns/registry.py:get_pattern` → e.g. `patterns/react.py` |
| `reflect` / `plan_execute` | per-pattern config, read by `_build_reflect` / `_build_plan_execute` in `patterns/__init__.py` |
| `sub_agents` / `aggregator_prompt` | composite patterns (`patterns/delegating.py`); sub-agents resolved in `runtime.py` and pinned in `manifests/pin.py` |
| `max_turns` / `recursion_limit` / `output_schema` / `extensions` | passed through `PatternBuildContext` by `builder.py`; `output_schema` only to patterns registered `honours_output_schema=True`; `extensions` is plugin-owned and core never reads inside it |
| `model` / `decider` | `patterns/model.py:build_model` and `felix/decisions.py` — see the model-layer skill |
| `system_prompt` / `prompts` | `builder.py`; `prompts` templates in `prompts/templates.py` |
| `tools` / `tool_guidance` | `ToolProvider.resolve` (`tools/provider.py`, builtins in `tools/builtins.py`); `tool_guidance` joins each tool's own `prompt_guidance` in the prompt section `builder.py:tool_guidance_section` writes |
| `tools_retrieval` | `tools/retrieval.py` (and `tools/decider_retrieval.py`), applied in `patterns/react.py` |
| `mcp_servers` | `mcp/client.py:tools_from_mcp_servers` → `server__tool` |
| `peers` | `a2a/peers.py:tools_from_peers` → `peer__name` |
| `delegation` | `tools/delegation.py:make_task_tool` → `task`; children compiled beside `sub_agents` in `builder.py` (`schema.child_agent_names`), pinned in `manifests/pin.py`; refused alongside `sub_agents` |
| `a2a` | the published agent card: `a2a/card.py`, served by `routes/well_known.py` |
| `browser_tools` / `sandboxes` / `containers` / `queues` / `client_tools` | `tools/{browser,sandboxes,queues,client_bridge}.py` |
| `shell_tools` / `workspace` | `tools/shell.py`, `shell_runner.py`, `security/shell_policy.py`; `tools/workspace_*.py` (and the outermost `apply_workspace_scope`); both have refusals in `manifests/governance.py` |
| `http_tools` / `search_tools` / `document_tools` / `image_tools` / `github_publish` | `tools/{http_fetch,web_search,document_search,image_tools,github_publish}.py` |
| `skills` / `skills_declared_only` / `personal_skills` | `felix/skills/loader.py` — catalog XML appended to the system prompt; personal skills also via `runtime.py` |
| `skill_suggestion` / `skill_authoring` | `skills/suggest.py` (read in `patterns/react.py`); `skills/{authoring,tools}.py` |
| `session` | `session/strategies.py` via `runtime.py:build_tenant_agent` |
| `memory` / `procedural_memory` | `memory/{capture,store,procedural}.py` |
| `policies`, `command_screening`, `content_screening`, `limits`, `guardrails`, `approvals`, `artifacts` | the `apply_*` wrappers in `builder.py` (fixed order — see the governance-pipeline skill) |
| `governance` | `manifests/governance.py:validate_governance` (compile-time) |
| `anomaly` | `jobs/anomaly.py` (the worker's scan), validated in `manifests/governance.py` |
| `observability` | `runtime.py`, `observability/metrics.py` |
| `auth.inbound` | `manifests/inbound_auth.py:enforce_inbound_auth` |
| `execution.mode: durable` | `durability/fibers.py` — `/chat` returns `202` + `resume_token` |

The authoritative list of fields is `schemas/manifest.schema.json` (the `Spec` definition). A field
missing from this table is not a field that does nothing — grep `spec.<field>` before concluding so.

See [references/spec-fields.md](references/spec-fields.md) for the per-block details and gotchas.

## Adding a new spec field

1. Add it to the model in `manifests/schema.py` with a sane default (absent field must not change
   behavior).
2. Consume it in `builder.py` — a binder (before the wrapper stack) or an `apply_*` wrapper (in the
   correct slot of the stack).
3. Exercise it in a bundled manifest and add a case to `tests/unit/test_manifest_schema.py` (shape)
   and `tests/unit/test_manifest_governance.py` (behavior, if it is a control).
4. Document it: `guide/manifest-reference.mdx` in the felix-web docs repo (see the docs-sync skill).

## Removing a spec field

A removal is not the mirror of an addition, because manifests are *stored*. The schema is
`extra=forbid`, so dropping a field retroactively invalidates every manifest already in the
store that set it — and the store is consulted ahead of bundled YAML, so a stale row also
shadows the file it came from. `spec.model.region` did this to `quick` on any deployment
whose copy predated #125: the default manifest answered every request with
`spec.model.region: Extra inputs are not permitted`.

1. Remove it from `manifests/schema.py` and its reader in `builder.py`.
2. Add the dotted path to `manifests/compat.py:RETIRED` with why and when. Stored manifests
   then load with a warning instead of failing; authored ones still fail, which is the point.
3. `make schema`, and a case in `tests/unit/test_stored_manifest_compat.py`.

**Only list a removal that is inert** — one where a manifest with the field and one without
compile to the same agent. A field that *did* something needs a migration that rewrites the
stored rows; dropping it silently would start an agent whose behaviour changed with nobody
told, which is worse than refusing to start it.

`RETIRED` expresses a removed **key**, and only one that is not inside a list and has a
single accepted spelling (an aliased field such as `spec.mcp` / `mcp_servers` needs every
spelling listed). It does **not** cover a field whose accepted *values* narrowed — a
`Literal` becoming a registry lookup, a new validator, a field becoming required. Those
brick stored rows the same way and need their own answer; `spec.memory.checkpointer` is the
example already in the tree.

## Validate — always

```bash
uv run felix validate-manifest manifests/<name>.yaml -e development
uv run felix validate-manifest manifests/<name>.yaml -e production   # governance-bearing
uv run felix bundle-manifests                                        # all bundled manifests still load
```

CI runs `bundle-manifests` before pytest, so a broken manifest fails the whole build.

`validate-manifest` runs the schema, the governance frameworks, the refusals `PUT /manifests`
makes (`validate_for_write`, so `ok` means the store would take it), the pattern registry (an
unregistered `spec.pattern` fails here, listing the registered names), and — unless
`--no-resolve-egress` — resolves the hostnames of `mcp_servers[].url`, `peers[].url` and
`containers[].gateway_url` and rejects blocked addresses. Other outbound URLs (`http_tools`,
`execution.webhooks`, queues) are checked at dial time, not here.

**What validation does not catch:** that an MCP server or peer actually answers. An unreachable
one binds zero tools at compile time with only a logged warning. Smoke the manifest against a
running API (`POST /chat` with `"manifest": "<name>"`) before calling it done.

## Session and memory internals

What a thread's event log appends and what it derives, which `spec.session` / `spec.memory` field
drives which module, and where the Postgres and `memory://` arms must agree:
[references/session-memory.md](references/session-memory.md). Read it before changing anything under
`felix/session/` or `felix/memory/`.
