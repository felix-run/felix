---
name: postgres-migrations
description: Author and apply Alembic migrations for Felix, including SQLAlchemy model changes, tenant RLS policies, pgvector columns, and Postgres FTS indexes, plus the in-memory store twin every new store needs. Use when changing db/models.py, adding a table, column, index, or RLS policy, when a migration fails, or when asked about the database schema.
compatibility: Requires uv; a running Postgres (make up) only for the live migrate/rollback check.
allowed-tools: Read Grep Glob Bash(uv run felix migrate:*) Bash(uv run alembic:*) Bash(./scripts/test.sh:*)
---

# Postgres migrations

Postgres is the system of record. Models in `packages/harness/src/felix/db/models.py` are **not**
auto-migrated — every model change needs a hand-written Alembic revision in the same change.

## Existing revisions

```
0001_baseline      0002_a2a_tasks     0003_session_tree
0004_usage_events  0005_session_fts   0006_tenant_rls
```

## Add a revision

1. Copy the shape of the nearest existing revision in `migrations/versions/`. Name it
   `000N_<slug>.py`, set `down_revision` to the current head, and write a real `downgrade()`.
2. Keep it **online-safe**: `CREATE INDEX CONCURRENTLY` where possible, no long exclusive locks, no
   rewriting a large table in one statement. New columns are nullable or have a default.
3. Tenant-scoped table? Mirror the policy pattern from `0006_tenant_rls.py`
   (`app.tenant_id` GUC per transaction) and make the store go through
   `db/session.py:tenant_session()`. RLS is opt-in via `FELIX_DATABASE_RLS`, so the store must be
   correct with the policy both on and off.
4. Vector column? Match the existing pgvector dimensions and index type used by `memory/store.py`.
   Full-text search extends `0005_session_fts.py` rather than adding a parallel mechanism.
5. **Never edit a revision already on `origin/main`** — a `PreToolUse` hook blocks it. History is
   applied in other environments; add a new revision instead.

## Apply and prove reversibility

```bash
make up                        # ask the user first — starts Postgres+pgvector and Valkey
uv run felix migrate head      # apply
uv run felix migrate 000<N-1>  # prove downgrade works
uv run felix migrate head      # back to head
uv run felix doctor            # connectivity + config check
```

The CLI points Alembic at the repo `alembic.ini`; run it from the repo root.

## The in-memory twin

CI has no database. `db/session.py:_use_memory` switches on `memory://`, and every store has an
in-memory implementation used by the whole test suite. A new store or query path needs both, or
`tests/unit/test_stores_memory.py` / `test_protocols_memory.py` will not be able to cover it.

```bash
./scripts/test.sh tests/unit/test_stores_memory.py tests/unit/test_protocols_memory.py
```

## Checklist before reporting

- [ ] Revision id + `down_revision` correct, `downgrade()` real and tested
- [ ] Model, store, and in-memory twin all updated
- [ ] Tenant scoping (`tenant_id` column + RLS policy) for anything tenant-owned
- [ ] Index for every new query shape
- [ ] `internals/persistence.mdx` in the felix-web docs updated (docs-sync skill)
