---
name: felix-architecture
description: How the Felix harness is put together — the manifest compile pipeline, the fixed governance wrapper order, the workspace layout, and the plugin seam. Use when changing anything under packages/harness, apps/api, or apps/worker, or when you need to know where a piece of behavior lives before editing it.
---

# Felix architecture

Felix is a self-hostable agents harness. A YAML manifest (`apiVersion: felix/v1`) is compiled at
request time into a governance-wrapped agent and served over REST/SSE, an OpenAI-compatible
`/v1`, A2A JSON-RPC, and MCP. Python 3.14, uv workspace, FastAPI + Granian, Taskiq worker and
scheduler, Postgres + pgvector, Valkey/Redis, pluggable object store.

## Workspace layout

- `packages/harness` (`felix`) — all the logic: manifests, patterns, tools, session, governance,
  auth, memory, eval, durability, storage, plugins.
- `packages/cli` (`felix`) — `migrate | eval | mint-jwt | bundle-manifests | validate-manifest |
  doctor | version | temporal-worker`.
- `apps/api` (`felix-api`) — FastAPI routes, one module per surface in `routes/`.
- `apps/worker` — Taskiq broker plus cron tasks. `felix-scheduler` must run alongside
  `felix-worker` or no periodic job fires.
- `manifests/` — bundled agents. The filename stem is the manifest name; there is no registry
  file. `manifests/governed.yaml` is the fullest example of the schema.
- `skills/<name>/SKILL.md` — agent skills, referenced from a manifest's `spec.skills`.

## The compile pipeline — understand this first

`manifests/builder.py:build_agent` is the center of the system. A request resolves a manifest
(Postgres store, then tenant object store, then global object store, then the bundled YAML file),
enforces inbound auth and the compile pin, then compiles:

1. Resolve the system prompt, sub-agents, and base tools from the `ToolProvider`.
2. Bind outbound tools from the spec: MCP servers become `server__tool`, A2A peers become
   `peer__name`, plus browser, sandbox, container, queue, and client tools.
3. Wire agent skills — the catalog XML is appended to the system prompt — and inject active
   memory facts.
4. Wrap every tool in the governance stack, in a fixed order.
5. Hand the result to a pattern builder from the open registry (`patterns/registry.py`).

## The wrapper order is load-bearing

    secret masking -> policies -> command screening -> content screening -> limits ->
    guardrails -> judges -> approvals -> artifact spill

Each wrapper clones the tool with a new executor, so the order defines precedence. The comment
`order matters` in `builder.py` is not decorative. Never reorder it to make a test pass.

## Trust is an allowlist

`Tool.executor.transport` is an open string. Governance decides trust by what is known safe —
`_TRUSTED_TRANSPORTS` is `{"local"}` — never by a denylist. Everything else (mcp, http, client,
sandbox, container, queue, peer) is untrusted and is content-screened. A denylist would fail open
for exactly the third-party transports the seam exists to allow.

## Extending it

- A new pattern is `register_pattern(...)` at import time. Nothing in core enumerates patterns.
- Anything that selects a swappable implementation is an open registry — `register_pattern`,
  `register_model_provider`, `register_object_store`, `register_secrets_backend`,
  `register_warehouse_backend`, `register_embedder_backend`, `register_session_strategy`,
  `register_checkpointer` — and the setting that selects one is an open `str` validated against
  that registry, never a closed `Literal`.
- Optional features attach through `felix/plugins.py` (registry or `felix.plugins` entry points).
  `apps/api/src/felix_api/composition.py` is the only place in core that may name a plugin. Core
  must never import an optional package directly.
- Adding a manifest field means: `manifests/schema.py`, then an `apply_*` wrapper or binder in
  `builder.py`, then a case in `tests/unit/`, then `make schema` to regenerate the checked-in
  `schemas/manifest.schema.json`.

## Protocols, not vendors

Postgres, the cache, the object store, secrets, model providers, and the warehouse are all
reached through Protocols with swappable implementations. AWS and GCP are optional extras; the
default path is `FELIX_OBJECT_STORE=fs` with zero cloud SDKs. Heavy dependencies live behind
extras and are imported lazily inside the function that needs them, never at module top level.

Postgres is the system of record. The warehouse is optional append-only spill, written after the
Postgres write.
