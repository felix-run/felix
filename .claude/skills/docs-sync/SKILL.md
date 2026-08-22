---
name: docs-sync
description: Keep Felix documentation true to the code across two repos — in-repo docs (README, CLAUDE.md, .env.example, deploy/GOVERNANCE.md, CHANGELOG, roadmap) and the public Starlight MDX site in the separate felix-web repo (apps/docs/src/content). Use after a feature lands, when the doc-drift Stop hook fires, before a release, or when asked to update, audit, or sync documentation.
compatibility: Public docs edits require a felix-web checkout (default ~/Projects/felix-web, override with FELIX_DOCS_ROOT).
allowed-tools: Read Grep Glob Bash(git:*) Bash(uv run felix:*)
---

# Docs sync

Documentation that describes an older design is worse than no documentation, because it is
believed. Verify every claim against the code before writing it — endpoint paths, env-var names,
CLI flags, scope names, and defaults get copied from source, never from memory.

## Where docs live

**This repo:** `README.md`, `CLAUDE.md`, `CONTRIBUTING.md`, `.env.example`,
`deploy/GOVERNANCE.md`, `deploy/docker/README.md`, `deploy/helm/README.md`, `CHANGELOG.md`,
`docs/roadmap.md`.

**Public site:** the **felix-web** repo — Starlight MDX under `apps/docs/src/content/`
(`guide/` = operators and integrators, `internals/` = mechanism). Default checkout
`~/Projects/felix-web/apps/docs`; override with `FELIX_DOCS_ROOT`. It is a **separate git repo**:
say so when you edit it, keep changes inside `src/content/`, and don't commit there unless asked.

Full surface → page table: [references/page-map.md](references/page-map.md).

## Procedure

1. **Scope** — the surfaces named in the prompt; else `git diff --name-only HEAD` plus untracked
   files; else sweep the mapped pages for stale claims.
2. **Map** each changed surface to its page(s) using the reference table.
3. **Verify** the current behavior in code. Read the route, the schema field, the setting default.
4. **Write** in the existing voice: dense, factual, present tense, identifiers in backticks, tables
   for surfaces and settings, no marketing language.
5. **Build** when you touched MDX: `pnpm --filter @felix/docs build` in the felix-web repo. Run
   `uv run felix bundle-manifests` when you quoted a manifest.

## Stale-truth traps specific to Felix

- The runtime is **self-hosted Python** — no Cloudflare Workers / Durable Objects / Hyperdrive /
  R2-binding compute. Cloudflare DNS/CDN/TLS/WAF in front of an origin is fine.
- Default object store is **`fs`**, not S3/MinIO. MinIO is `--profile full` only.
- **Postgres is the system of record**; the warehouse is optional append-only spill, default `none`.
- **`felix-scheduler` is required alongside `felix-worker`** or no cron task fires.
- Model ids: check `DEFAULT_MODEL_ROUTES` in `felix/config.py` before naming a model anywhere.
- Auth: `require_mgmt_scopes` is a no-op when `FELIX_AUTH_MODE=none` — never document that as
  "protected by default".
- Tests: the supported no-infrastructure path is `FELIX_DATABASE_URL=memory://ci`.

## Report

Drift table (`surface → page`, was → now), files edited with the repo labeled on each line,
verification commands with real output, and anything left stale deliberately with the reason.
