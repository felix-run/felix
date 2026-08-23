# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **Audit and usage events emitted while serving traffic were never persisted.**
  `emit_agent_audit` and `record_usage` are called from the agent loop, which runs in the
  **API** process, but the only `flush_pending` callers were Taskiq cron tasks in the
  **worker**. Wherever those are separate containers — Compose, Helm, the documented
  deploy paths — the worker drained an always-empty buffer while the API's grew for the
  life of the process. So `GET /audit` returned only worker-side events, metered usage
  was lost, and the API leaked memory in proportion to tool calls. The API now runs its
  own flush loop (`FELIX_AUDIT_FLUSH_SECONDS`, default 5s) and drains on shutdown.
- **A failed flush no longer discards the batch.** Both stores drained the buffer
  *before* writing, so one `commit()` failure lost those events permanently — for audit,
  that is the compliance record. Batches are now re-queued in order and retried.
- Buffers are bounded (10k events) and count what they drop, so an unreachable database
  degrades visibly instead of exhausting the process silently.
- `emit_agent_audit` logged nothing when recording failed (`except Exception: pass`); it
  now warns.

### Added

- Security scanning: CodeQL, a `pip-audit` CVE check over the locked
  dependency set (all extras), a gitleaks secret scan of the full history, and
  a Trivy scan of the image CI builds.
- Test coverage is measured and gated at the current 60%, ratcheted upward
  deliberately rather than set aspirationally.
- `tests/unit/test_invariants.py` — the repo rules are now enforced rather than
  documented: `.env.example` covers every `Settings` field, no optional
  dependency is imported at module scope, every Postgres-touching module has a
  `memory://` path, and the governance wrapper order in `builder.py` is fixed.
- `scripts/lean-import-check.py` and a CI `lean` job that imports all 156
  modules with no extras installed — the default image's promise, checked.
- `scripts/validate-toolkit.py` and a CI `toolkit` job; `.claude/**` and
  `CLAUDE.md` are now inside the CI path filter instead of bypassing every gate.
- Six settings that existed only in `config.py` are documented in
  `.env.example`: `FELIX_DATABASE_RLS`, `FELIX_SCALE_OUT`, `FELIX_REPLICA_ID`,
  `FELIX_OTEL_ENDPOINT`, `FELIX_WEBHOOK_SECRET`, and `FELIX_POLICY_BUNDLE_PUBKEY`
  (the last is declared but not yet consumed by any code path).
- GitHub Actions are pinned by commit SHA, and all container base/service
  images by digest, so a rebuild is reproducible and a retagged upstream image
  cannot change what ships.
- `scripts/test.sh` — the canonical test entry point. It sets the in-memory
  store environment the suite is designed for; `make test` and CI both use it.
- `pre-commit` now runs in CI, so the hook config cannot silently break again.
- `.editorconfig` matching the ruff configuration.
- `make type` now says what to do when the optional extras are missing instead
  of printing 27 unresolved-import errors.

### Changed

- Dependabot: weekly grouped updates for actions and images; the docker
  ecosystem now points at `deploy/docker` (the previous `/` entry matched
  nothing — there is no Dockerfile at the repo root).
- Builder image `uv` 0.9 → 0.12, `ty` 0.0.73 → 0.0.74, and the Docker build
  caches uv downloads between builds.
- Relicensed from MIT to Apache License 2.0 (adds an express patent grant and
  a trademark carve-out; contributions are inbound under the same license).
  Adds a `NOTICE` file; releases published under MIT remain MIT.

### Fixed

- The runtime image no longer ships `pip`. Its vendored copies of `msgpack`
  and `setuptools` carried HIGH CVEs (GHSA-6v7p-g79w-8964, CVE-2025-47273)
  even though neither is a Felix dependency; the venv is built by uv in the
  builder stage, so the runtime never needed pip. The runtime stage also
  applies pending OS security updates, clearing four util-linux CVEs that
  `python:3.14-slim` has not picked up yet. The image scans clean.
- `pre-commit install` failed for every contributor: the ruff repo entry was
  missing its `https://github.com/` prefix, so hook installation could never
  clone it. `pre-commit validate-config` passes on the broken file — only
  `install-hooks` surfaces it.
- `make check` failed on any machine with a `.env`: the pytest leg inherited
  `FELIX_DATABASE_URL` and ran against a real Postgres, and `make type`
  checked `tests/` while CI checks only `packages apps`.
- The Docker build no longer falls back to an unfrozen `uv sync`, which could
  silently produce an image from a different dependency resolution than CI
  tested. CI now also verifies `uv.lock` is current and installs `--frozen`.
- Taskiq worker no longer dies on idle BRPOP (`redis-py` 8 default
  `socket_timeout=5`); broker/result backend use `socket_timeout=None`.
- Scheduler entrypoint awaits `run_scheduler` via `asyncio.run` (taskiq 0.12+).
- Worker/scheduler Compose healthchecks disabled (image probe targets API `/health`).

### Changed

- Session leases prefer Redis (with in-process fallback) so exclusive/shared
  attach works across API replicas.

### Added

- Session control routes: snapshots, FTS search, abort/continue, thinking
  levels, leases, compact, UI prompts, JSONL export (see README Protocols).

## [0.1.0] — 2026-08-22

### Added

- Initial public release of **Felix** — self-hostable managed agents harness
  (`apiVersion: felix/v1`).
- Surfaces: `/chat`, OpenAI-compatible `/v1`, A2A, MCP, management APIs.
- Lean Docker Compose (api, worker, **scheduler**, Postgres+pgvector, Valkey)
  with optional MinIO (`--profile full`).
- Helm chart with PVC support, consumer shared secret, scheduler container, and
  pre-install/pre-upgrade **migrate Job**.
- Durable fibers, audit spill to DuckDB (optional), JWT/api_key auth, plugins seam.
- Durable **usage meters** (`usage_events`) flushed by the worker; `GET /usage`.
- Eval **fixture + `--mock`** path for CI (`fixtures/eval/smoke.json`).
- Chat **history** (`GET`/`DELETE /chat/history/{thread_id}`) and **audit metrics**.
- Response aliases (`events`/`plans`/`requests`/`manifests`/`datasets`) for chat-ui clients.
- CLI: `migrate`, `eval`, `mint-jwt`, `bundle-manifests`, `doctor`, `version`, `temporal-worker`.
- Typed packages (`py.typed`) for harness, CLI, API, and worker.

[0.1.0]: https://github.com/felix-run/felix/releases/tag/v0.1.0
