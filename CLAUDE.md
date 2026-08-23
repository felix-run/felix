# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Felix is a self-hostable **agents harness**. A YAML manifest (`apiVersion: felix/v1`)
is compiled at request time into a governance-wrapped `Agent` and served over REST/SSE,
an OpenAI-compatible `/v1`, A2A JSON-RPC, and MCP. Python 3.14, uv workspace, FastAPI +
Granian (API), Taskiq (worker/scheduler), Postgres+pgvector, Valkey/Redis, pluggable object store.

## Commands

```bash
make install            # uv sync --dev (lean core; what CI uses)
make install-full       # uv sync --all-extras --dev (aws/gcp/mcp/browser/embeddings/…)
make check              # ruff check + ty check + pytest + ruff format --check
make lint / fmt / type / test
make dev                # API on :8080 with FELIX_AUTH_MODE=none, fs object store
make cli                # httpx REPL client (clients/cli.py)
make migrate            # felix migrate head (Alembic)
make doctor             # felix doctor — config/connectivity preflight
make up / up-lite / up-gcp / up-full   # Compose overlays under deploy/docker/
```

`make type` and CI both run `ty check packages apps`; tests are excluded on purpose (fakes trip `ty`).
Both need the optional extras installed (`make install-full`) — unresolved imports are errors, so a
lean venv reports every optional dependency as one. `make type` checks for this and says so.

### Running tests

The repo `.env` points `FELIX_DATABASE_URL` at a real Postgres, and pydantic-settings
reads it, so a bare `uv run pytest` fails on DB-touching tests. `./scripts/test.sh` sets the
in-memory environment and is what `make test` and CI both run:

```bash
./scripts/test.sh                                   # full suite (~12s)
./scripts/test.sh tests/unit/test_react_loop.py -q  # one file
./scripts/test.sh -k compact                        # one theme
make check                                          # lint + type + test + format check
```

`memory://` in the DB URL flips every store to its in-memory implementation
(`felix/db/session.py:_use_memory`, `felix/session/store.py:get_session_store`) — that is
the supported no-infrastructure test path, not a mock layer.

Structural gates (fast, no infrastructure):

```bash
./scripts/test.sh tests/unit/test_invariants.py   # repo invariants, enforced
uv sync --locked --no-dev && uv run --no-sync python scripts/lean-import-check.py
python3 scripts/validate-toolkit.py               # .claude/ hooks, settings, skills
uv run python scripts/gen-manifest-schema.py --check   # editor JSON Schema is current
```

`tests/unit/test_invariants.py` turns the rules below into failures: `.env.example` covers every
`Settings` field, no optional dependency is imported at module scope, every Postgres-touching module
has a `memory://` path, the governance wrapper order is unchanged, and `schemas/manifest.schema.json`
still matches the pydantic models. Change a rule deliberately and you update the test with it.

Eval smoke (no model calls): `uv run felix eval --dataset smoke --manifest quick --fixture fixtures/eval/smoke.json --mock`.

## Architecture

### Workspace layout

- `packages/harness` (`felix`) — all the logic: manifests, patterns, tools, session,
  governance, auth, memory, eval, durability, storage, plugins.
- `packages/cli` (`felix`) — `migrate | eval | mint-jwt | bundle-manifests | validate-manifest | doctor | version | temporal-worker`.
- `apps/api` (`felix-api`) — FastAPI routes, one module per surface in `routes/`.
- `apps/worker` (`felix-worker`, `felix-scheduler`) — Taskiq broker + cron tasks.
- `manifests/` — bundled agents (`quick`, `deep`, `router`, `governed`, …); `governed.yaml` is the fullest example of the schema.
- `skills/<name>/SKILL.md` — Agent Skills, referenced from a manifest's `spec.skills`.

### The compile pipeline (the thing to understand first)

`manifests/builder.py:build_agent` is the center of the system. A request resolves a
manifest (`runtime.py:resolve_tenant_manifest` → `manifests/resolver.py`, DB store then
bundled YAML), enforces inbound auth and the compile pin, then compiles:

1. Resolve system prompt, sub-agents, and base tools from the `ToolProvider`.
2. Bind outbound tools from the spec: MCP servers → `server__tool`, A2A peers →
   `peer__name`, browser/sandbox/container/queue/client tools, procedural-memory writer.
3. Wire Agent Skills (catalog XML appended to the prompt) and inject active memory facts.
4. **Wrap every tool in the governance stack, in a fixed order** — secret masking →
   policies → command screening → content screening → limits → guardrails → judges →
   approvals → artifact spill. The comment `order matters` is load-bearing; each wrapper
   clones the tool with a new executor, so order defines precedence.
5. Hand the result to a pattern builder from the open registry
   (`patterns/registry.py`; `patterns/react.py:build_react_agent` is the main one) via a
   plain-dict `PatternBuildContext`.

Adding a manifest field means: `manifests/schema.py` → an `apply_*` wrapper or binder in
`builder.py` → a case in `tests/unit/` → `make schema` (regenerates the generated, checked-in
`schemas/manifest.schema.json` that the `# yaml-language-server` header in every manifest points at).
Adding a pattern means `register_pattern(...)` at import time — nothing in core enumerates patterns.

### Protocols, not vendors

The harness talks to Postgres, a cache, an object store, secrets, model providers, and the
warehouse through Protocols with swappable implementations (`storage/{fs,s3,gcs}.py`,
`secrets.py`, `patterns/model.py:ModelProvider`, `warehouse.py`). AWS and GCP are optional
extras; the default path is `FELIX_OBJECT_STORE=fs` with zero cloud SDKs. Keep it that way —
heavy deps (Playwright, sentence-transformers, DuckDB, Presidio, Temporal) live behind
extras and are imported lazily inside functions, never at module top level.

There is deliberately **no** Cloudflare Workers / Durable Objects / Hyperdrive / R2-binding /
Queues compute in this stack (Cloudflare DNS/CDN/WAF in front of an origin is fine).

### Plugin seam

`apps/api/src/felix_api/composition.py` is the only place in core that may name plugins;
optional packages register routes, tools, authenticators, cron tasks, and rate-limit keys
through `felix/plugins.py` (registry or `felix.plugins` entry points). Core must never
`import felix_commerce` / `felix_enterprise` — `tests/unit/test_plugin_boundary.py` asserts this.
New optional features belong behind that seam, not in `felix` core.

### Request path and state

`create_app` (apps/api) stacks body-limit → rate-limit → `AuthMiddleware`, stores
`settings`/`tools`/`plugins` on `app.state`, and mounts route modules plus plugin routers.
Management endpoints gate on scopes via `auth/mgmt.py:require_mgmt_scopes` (skipped entirely
when `auth_mode=none`; `admin`/`*` bypass; `x:write` implies `x:read`).

Chat state is an append-only session event log (`session/store.py`) with strategies
(`full_replay`, `compacting`, `windowed:N`, `semantic:N`) plus fork/rewind/lease/search/export.
`spec.execution.mode: durable` enqueues a fiber (`durability/fibers.py`, Temporal optional)
and returns `202` + `resume_token`.

The worker owns everything periodic: audit/usage flush, scheduled jobs, memory consolidation,
retention, anomaly scan, continuous eval, fiber resume (`apps/worker/.../tasks.py`, Taskiq
cron labels). `felix-scheduler` must run alongside `felix-worker` or nothing fires.

## Conventions

- All settings are pydantic-settings on `felix/config.py:Settings` with the `FELIX_` env
  prefix; add new ones there and mirror them in `.env.example` and the README.
- Optional imports go inside the function that needs them, wrapped in `try/except` with a
  `logger.warning` when a binding failure should degrade rather than fail the build.
- ruff line-length 110, `target-version = py314`; the `ignore` list in `pyproject.toml`
  documents why each rule is off — read it before "fixing" an E731 or SIM102.
- Postgres is the system of record; the warehouse (`FELIX_WAREHOUSE`) is optional
  append-only spill written after the Postgres write.
- `docs/roadmap.md` tracks in-flight work and is expected to be updated in place.

## Claude Code toolkit

`.claude/` holds project-scoped subagents, skills, and hooks tuned to this repo — see
[.claude/README.md](.claude/README.md) for the full map. The parts that change how you work:

- **Run tests with `./scripts/test.sh`** (or `make test`) — a `PreToolUse` hook blocks a bare
  `pytest`, which would hit the `.env` Postgres.
- **Skills load on demand** for the deep procedures: `felix-dev-loop`, `manifest-authoring`,
  `governance-pipeline`, `api-surface`, `postgres-migrations`, `plugin-seam`, `security-review`,
  `docs-sync`, `deploy-runbook`, `python-conventions`, `branch-pr-workflow`, `code-quality`,
  `dead-code-audit`, `test-quality`.
- **Subagents** for delegated work: `felix-engineer`, `felix-postgres`, `felix-devops`,
  `felix-code-reviewer`, `felix-security-reviewer`, `felix-manifest-architect`,
  `felix-test-engineer`, `felix-dx-maintainer`, `felix-docs-syncer`, `felix-quality-reviewer`,
  `felix-test-quality-reviewer`.
- **Hooks** format edited Python with ruff, validate changed manifests, name the companion file or
  docs page a change requires, report a `.py` whose complexity got worse than at `HEAD`, and gate
  the end of a turn when documented surfaces drifted.
- Public docs live in the separate **felix-web** repo (`apps/docs/src/content/`, Starlight MDX);
  set `FELIX_DOCS_ROOT` if your checkout is not at `~/Projects/felix-web/apps/docs`.
