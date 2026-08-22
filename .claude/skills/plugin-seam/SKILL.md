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

| Capability | Hook |
|---|---|
| Routes | `plugin.routes(app, tools=…)` or `registry.register_router()` |
| Tools | `plugin.register_tools(register)` |
| Auth modes | `registry.register_authenticator(mode, builder)` |
| Cron tasks | `plugin.cron_tasks` → registered by the worker at startup |
| Rate-limit keys | `plugin.rate_limit_key(request)` |
| Body limits | `plugin.body_limit_bytes` |
| Self-authenticating mounts | `plugin.self_authenticating_mounts` |
| Audit / usage sinks | `registry.register_audit_sink_factory()` / `register_usage_sink_factory()` |
| Startup hooks | `registry._startup_hooks` (awaited in the API lifespan) |

The `FelixPlugin` Protocol in `plugins.py` documents the full shape.

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
5. Verify: `./scripts/test.sh tests/unit/test_plugin_boundary.py` and a lean install
   (`uv sync --dev`) with the feature absent — core must still import and serve.
