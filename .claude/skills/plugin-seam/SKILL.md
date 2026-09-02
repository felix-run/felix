---
name: plugin-seam
description: How optional features attach to Felix without polluting core — the plugin registry, felix.plugins entry points, the composition wiring root, and the lean-default rule for heavy dependencies and extras. Use when adding an optional feature, an extra, a new tool provider, an authenticator, a cron task, or when the plugin-boundary test fails.
allowed-tools: Read Grep Glob Bash(uv:*) Bash(./scripts/test.sh:*)
---

# The plugin seam

Felix core must never import an optional feature package. Optional packages attach themselves at
startup through the registry in `packages/harness/src/felix/plugins.py` or a `felix.plugins` entry
point.

**`apps/api/src/felix_api/composition.py` is the only core file allowed to name a plugin.** It is
the composition root: `installed_plugins()` discovers them, `compose()` builds the process-wide
`ToolProvider` and registers builtins plus plugin tools. Removing a feature = deleting its entry or
uninstalling the package. `tests/unit/test_plugin_boundary.py` asserts both this and the absence of
`felix_commerce` / `felix_enterprise` imports anywhere in `felix` or `felix_api`.

## What a plugin can register

On the registry / plugin object:

| Capability | Hook |
|---|---|
| Routes | `plugin.routes(app, tools=…)` |
| Tools | `plugin.register_tools(register)` |
| Auth modes | `registry.register_authenticator(mode, builder)` + `FELIX_AUTH_MODE=<mode>` |
| Cron tasks | `plugin.cron_tasks` → registered by the worker at startup |
| Rate-limit keys | `plugin.rate_limit_key(request)` |
| Body limits | `plugin.body_limit_bytes` |
| Self-authenticating mounts | `plugin.self_authenticating_mounts` |
| Audit / usage sinks | `registry.register_audit_sink(factory)` / `register_usage_sink(factory)` |
| Startup hooks | `registry.register_startup_hook(hook)` (awaited in the API lifespan) |
| Agent-loop hooks | `registry.register_before_turn` / `filter_history` / `before_compact` / `before_tool` / `after_tool` / `compact_failed` |

Open registries in core, called at import time (not on the registry object):

| Capability | Hook |
|---|---|
| Patterns | `felix.patterns.registry.register_pattern(name, builder)` → `spec.pattern` |
| Model providers | `felix.patterns.model_registry.register_model_provider(name, factory)` |
| Object stores | `felix.storage.register_object_store(name, factory)` → `FELIX_OBJECT_STORE` |
| Secrets backends | `felix.secrets.register_secrets_backend(name, factory)` → `FELIX_SECRETS_BACKEND` |
| Warehouses | `felix.warehouse.register_warehouse_backend(name, factory)` → `FELIX_WAREHOUSE` |
| Embedders | `felix.memory.embedder.register_embedder_backend(name, factory)` → `FELIX_MEMORY_EMBEDDER` |
| Session strategies | `felix.session.strategies.register_session_strategy(prefix, factory)` → `spec.session.strategy` |
| Checkpointers | `felix.session.store.register_checkpointer(name, factory)` → `spec.memory.checkpointer` |

Manifest config: `spec.extensions.<plugin-name>` is the one field exempt from
`extra="forbid"`; it reaches a pattern builder as `PatternBuildContext["extensions"]`.

The `FelixPlugin` Protocol in `plugins.py` documents the full shape, and
`examples/felix-plugin-example/` is a working package that exercises every row above.

**Not extensible, deliberately:** the nine-wrapper governance order in `builder.py` is
fixed (`before_tool` / `after_tool` hooks are the sanctioned boundary, outside the
stack), and `Guardrails.providers` is closed — see `docs/ROADMAP.md` for the rationale.

## The lean-default rule

The default install and the default Docker image must stay small enough for a 2–4 GiB VM.

- Heavy dependencies — Playwright, sentence-transformers, DuckDB, ClickHouse/Doris clients,
  Presidio, Temporal, boto3, google-cloud-* — go in an **optional extra**, never in core deps.
- Import them **inside the function that needs them**, not at module top level:

  ```python
  if m.spec.browser_tools:
      try:
          from felix.tools.browser import tools_from_browser_refs

          _append_unique_tools(resolved, tools_from_browser_refs(...))
      except Exception:
          logger.warning("browser tool binding failed", exc_info=True)
  ```

- Add the extra to the owning package's `pyproject.toml`, forward it from the root
  `[project.optional-dependencies]` so `uv sync --extra <name>` works from the repo root, run
  `uv lock`, and add a row to the README extras table.
- Docker: extras enter the image only via `FELIX_DOCKER_EXTRAS`; the base image gets nothing new.

## Adding an optional feature

1. Decide it is genuinely optional — if core cannot function without it, it is not a plugin.
2. Build it as its own package (or extra-gated module) exposing a `FelixPlugin`.
3. Register via a `felix.plugins` entry point so `load_optional_plugins()` discovers it, or add the
   single line in `composition.py` if it ships in this repo.
4. Gate any settings behind `FELIX_` config with an off-by-default value.
5. Verify:
   ```bash
   ./scripts/test.sh tests/unit/test_plugin_boundary.py tests/unit/test_invariants.py
   uv sync --locked --no-dev && uv run --no-sync python scripts/lean-import-check.py
   ```
   The lean check imports every module with no extras installed — the same thing CI's `lean`
   job does, and the only way a module-scope optional import is caught before an operator hits it.

## A tool handler decorator must use `functools.wraps`

`define_tool` and `wrap_executor` decide how to call your function by reading its signature
once, at definition time (`felix/tools/types.py:accepts_positional`). A decorator that forwards
`*args, **kwargs` without `@functools.wraps` reports *its own* signature, so a one-argument
handler behind it is called with two and raises.

```python
@functools.wraps(fn)  # required: signature() follows __wrapped__
async def wrapper(*a, **k):
    return await fn(*a, **k)
```

This used to "work" by accident: the harness called wide, caught the `TypeError`, and retried
narrow — running the decorator's body twice for one tool call. That probe is gone, because it
could not tell a wrong arity from a `TypeError` raised inside a handler that had already run.
The failure is now loud and once.
