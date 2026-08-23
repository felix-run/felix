# felix-harness

The Felix harness — all of the logic behind the agents harness. `apps/api` and `apps/worker` are thin
processes on top of this package; almost every substantive change lands here.

This file is a **map**, not an explanation: 137 modules across 23 subsystems is more than filenames
can orient you to. For how the system works, read
[docs.felix.run/internals](https://docs.felix.run). For working conventions, read
[`../../CLAUDE.md`](../../CLAUDE.md).

## Start here

**[`src/felix/manifests/builder.py`](src/felix/manifests/builder.py) → `build_agent`** is the centre
of the system. A request resolves a manifest, enforces inbound auth and the compile pin, binds tools,
wraps every tool in the governance stack, and hands the result to a pattern builder. Reading that one
function explains more of this package than any other file.

The request path through the package:

```text
runtime.py → manifests/resolver.py → manifests/builder.py:build_agent → patterns/react.py
```

## Subsystem map

| Directory | Owns | Read first |
|---|---|---|
| `manifests/` | `felix/v1` schema, resolution, and the compile pipeline | `builder.py`, `schema.py` |
| `patterns/` | Agent loops and the model client; open registry | `registry.py`, `react.py`, `model.py` |
| `tools/` | Tool binding and execution: builtins, browser, sandboxes, queues, retrieval, client bridge | `executor.py`, `builtins.py` |
| `session/` | Append-only event log, strategies, fork/rewind/lease/search/export | `store.py` |
| `governance/` | Screening controls applied by the wrapper stack | `pii.py` |
| `security/` | Egress, expression evaluation, rate limiting, screening primitives | `expr.py` |
| `auth/` | Auth modes, JWT, middleware, management scopes | `middleware.py`, `mgmt.py` |
| `approvals/` | Human-in-the-loop pause and resume | — |
| `memory/` | Durable facts, capture, consolidation, procedural memory | `store.py`, `capture.py` |
| `skills/` | Agent Skills loading and catalogue injection | `loader.py` |
| `durability/` | Fibers and durable execution (Temporal optional) | `fibers.py` |
| `db/` | SQLAlchemy models, session factory, the `memory://` switch | `models.py`, `session.py` |
| `storage/` | Object store Protocol and `fs` / `s3` / `gcs` implementations | `fs.py` |
| `jobs/` | Scheduled work: retention, anomaly scan, continuous eval | `scheduler.py` |
| `eval/` | Datasets, fixtures, comparison, the `--mock` path | `runner.py` |
| `audit/`, `usage/` | Audit rows and usage meters, flushed by the worker | — |
| `a2a/`, `mcp/` | Outbound and inbound A2A and MCP protocol support | — |
| `observability/` | Logging and tracing wiring | — |
| `plans/`, `prompts/`, `ui/` | Plan objects, prompt assembly, UI prompt surface | — |

Loose top-level modules that are easy to miss:

| Module | Purpose |
|---|---|
| `config.py` | **Every** setting: `Settings` (pydantic-settings, `FELIX_` prefix) and `validate_runtime()` |
| `plugins.py` | The plugin registry and `felix.plugins` entry points |
| `sdk.py` | `FelixClient` — the Python client (`prompt`, `stream`, `steer`, `fork`, `rewind`) |
| `runtime.py` | Tenant and manifest resolution on the request path |
| `secrets.py` | Secrets Protocol: `env` / `file` / `aws` / `gcp` backends |
| `warehouse.py` | Optional append-only analytics spill, written after the Postgres write |
| `steer.py`, `waiters.py`, `buffers.py`, `side_events.py` | Live-run steering and streaming plumbing |
| `limits.py`, `artifacts.py`, `hooks.py`, `flush.py` | Run budgets, output spill, lifecycle hooks, batched writes |
| `context.py`, `context_files.py`, `embeddings.py` | Run context, file context, embedding backends |

## Rules that apply when editing here

These are enforced by tests, not convention — `tests/unit/test_invariants.py` and
`tests/unit/test_plugin_boundary.py` turn each into a failure.

- **Optional dependencies are imported inside the function that needs them**, never at module scope.
  Playwright, DuckDB, Presidio, Temporal, and the cloud SDKs must stay out of the lean install.
- **Every Postgres-touching module needs a `memory://` twin.** That is the CI test path, not a mock
  layer — and the twin is a second implementation, not duplication to be factored away.
- **The governance wrapper order in `builder.py` is load-bearing.** Each wrapper clones the tool with
  a new executor, so order defines precedence.
- **Core never names an optional plugin.** `apps/api/.../composition.py` is the only place; everything
  else goes through `plugins.py`.
- **Protocols, not vendors.** Storage, secrets, model providers, and the warehouse are swappable
  implementations behind Protocols.
- **A change to `db/models.py` needs an Alembic revision**, and published revisions are never edited.
- **A new `FELIX_` setting** lands in `config.py`, `.env.example`, and the README table together.

## Working on this package

Run everything from the repo root, not from this directory:

```bash
make install-full             # extras; the type check cannot resolve them otherwise
make check                    # ruff + ty + pytest + format check (matches CI)
./scripts/test.sh -k <expr>   # one theme; sets the in-memory stores the suite needs
```

Never a bare `pytest` — it reads the repo `.env` and fails against a real Postgres. See
[`../../docs/TROUBLESHOOTING.md`](../../docs/TROUBLESHOOTING.md).
