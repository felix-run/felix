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

The list moves every few PRs, so read it rather than trusting a copy:

```bash
ls migrations/versions/ | sort | tail -3     # the head is the last one
```

`0001_baseline` is the schema at the start; `0006_tenant_rls.py` is the RLS pattern to copy and
`0005_session_fts.py` the full-text search one.

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

## Prove the twin behaves like Postgres

`tests/unit/test_invariants.py` checks that every Postgres-touching module *has* a `memory://`
twin, not that the twin *behaves* like it, and CI's main suite only ever runs the twin. A unique
constraint the twin does not enforce is invisible there: an eval store that overwrote in memory
raised `UniqueViolation` on its second real write, and the scheduled sweep that re-writes it every
tick reported success while scoring nothing.

Run the feature once against a throwaway database, then encode the finding as a conformance arm:

```bash
docker exec felix-postgres-1 sh -c 'psql -U "$POSTGRES_USER" -d postgres -c "CREATE DATABASE scratch_x;"'
docker port felix-postgres-1 5432          # host port; the password is $POSTGRES_PASSWORD in the container
FELIX_DATABASE_URL=postgresql+psycopg://felix:<pw>@127.0.0.1:<port>/scratch_x \
  FELIX_DATABASE_RLS=false uv run felix migrate head
# ... exercise the store ...
docker exec felix-postgres-1 sh -c 'psql -U "$POSTGRES_USER" -d postgres -c "DROP DATABASE scratch_x;"'
```

The contract goes in `tests/conformance/`, parametrized on the `store_settings` fixture so the
same assertions run on both arms. Prove it by reverting the fix: the **postgres** arm fails while
the memory arm passes.

```bash
FELIX_CONFORMANCE_DATABASE_URL=postgresql+psycopg://felix:<pw>@127.0.0.1:<port>/scratch_x make conformance
```

## Checklist before reporting

- [ ] Revision id + `down_revision` correct, `downgrade()` real and tested
- [ ] Model, store, and in-memory twin all updated
- [ ] A conformance arm for a new or changed store, run once against a real database
- [ ] Tenant scoping (`tenant_id` column + RLS policy) for anything tenant-owned
- [ ] Index for every new query shape
- [ ] `internals/persistence.mdx` in the felix-web docs updated (docs-sync skill)
