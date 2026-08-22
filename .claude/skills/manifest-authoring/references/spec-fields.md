# felix/v1 spec blocks — details and gotchas

Authoritative source: `packages/harness/src/felix/manifests/schema.py`. This file records the
behavior you cannot see from the schema alone.

## Patterns

`spec.pattern` resolves through the open registry. Builders call `register_pattern(name, build,
kind=...)` at import time, so a pattern only exists if its module is imported (see
`patterns/__init__.py`). An unregistered name fails compile with `Unknown pattern '<x>' … registered: …`.
Multi-agent patterns declare `kind="multi-agent"` and use `spec.sub_agents` + `spec.aggregator_prompt`;
when `sub_agents` is set, the manifest's own `tools` list is **not** resolved.

## Tools and outbound integrations

- Built-in ids come from `tools/builtins.py` via the process-wide `ToolProvider` built in
  `apps/api/src/felix_api/composition.py`.
- Outbound bindings are best-effort: each block is wrapped in `try/except` that logs a warning and
  continues. A typo in an MCP URL yields an agent with fewer tools, not an error — check the logs.
- `mcp_servers[].auth: secret:NAME` and every other credential must be a `secret:` reference.
  `governance.forbid_plaintext_secrets` turns an inline secret into a compile error.
- Plain `http://` outbound URLs are rejected unless `environment=development` **and**
  `allow_insecure=true`.

## Skills

`spec.skills` loads `skills/<name>/SKILL.md` (Agent Skills format) and appends a catalog block to
the system prompt. Progressive disclosure is via the `list_skills` / `activate_skill` /
`deactivate_skill` tools — declaring skills without those tools still injects the catalog, since
the builder appends the skill tools when `spec.skills` is non-empty.

## Session strategies

| Strategy | Behavior |
|---|---|
| `full_replay` | default; whole event log replayed |
| `compacting` | token-threshold compaction; honors `reserve_tokens`, `keep_recent_tokens`, `context_window_tokens`, `compaction_enabled` |
| `windowed:N` | last N events |
| `semantic:N` | embedding-ranked N events — needs `felix-harness[embeddings]` |

Defaults when the block is absent: `full_replay`, reserve 16384, keep_recent 20000, window 128000.

## Memory

`memory.store: pgvector|memory|none`, `memory.checkpointer: postgres`. When the store is not `none`
and capture is enabled, `memory/capture.py:active_facts_prompt` injects durable facts into the
system prompt at compile time. `procedural_memory.enabled` adds a `remember_procedure` tool; recall
happens per turn inside the ReAct loop.

## Governance blocks

- `policies[]` — per-tool required scopes; enforced inside the tool wrapper, so a policy violation
  surfaces as a tool error, not an HTTP 403.
- `limits` — `max_tool_calls`, `max_wall_clock_seconds`; enforced per run.
- `approvals[]` — pause the run until a decision arrives (`/approvals`); `allow_unattended: false`
  means a durable/unattended run cannot self-approve. `ttl_seconds` bounds the wait.
- `content_screening` — screens **untrusted** tool output (MCP, A2A, browser, queues, sandboxes;
  `_is_untrusted_tool` in `builder.py`). `on_flag: quarantine|block|warn`. An optional `model:`
  turns on the LLM screener; marker-only is the default.
- `command_screening` — shell/command-shaped tool arguments; `include_defaults: true` pulls the
  built-in deny set.
- `guardrails.providers: [pii]` + `targets: [input, output]` — Presidio needs `felix-harness[pii]`.
  `judges` are opt-in LLM scoring; final-response judges wrap the agent itself.
- `anomaly.enabled` — worker-side scan (`jobs/anomaly.py`), needs `felix-scheduler` running.
- `governance` — `frameworks: [soc2, eu_ai_act]`, `risk_tier`, `transparency_notice` (prepends a
  notice to the system prompt), `forbid_plaintext_secrets`, `pin_compile` (pins the manifest
  version per thread via `manifests/pin.py`), `retention_days`.

## Inbound auth

`spec.auth.inbound`: `allow_anonymous`, `schemes: [jwt, api_key]`, `required_scopes`. Enforced by
`enforce_inbound_auth` **before** the agent is built. With `FELIX_AUTH_MODE=none` the caller is
anonymous, so a manifest with `allow_anonymous: false` is unreachable locally by design.

## Durable execution

`spec.execution.mode: durable` enqueues a fiber and returns `202` with a `resume_token`; poll
`GET /chat/runs/{resume_token}`. `FELIX_DURABILITY=temporal` swaps the backend (needs the
`temporal` extra and a running `felix temporal-worker`). Steering (`/chat/steer`) and follow-ups
work against both.
