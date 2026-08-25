# Upgrading a deployment

Nothing upgrades itself. [`RELEASING.md`](RELEASING.md) cuts a tag; no workflow builds an image from it and no
workflow deploys one. Every production upgrade is a deliberate act, and this is the procedure for
performing one.

The rule that shapes all of it: **a migration and the image that expects it are one change, not
two.** Alembic revisions here are linear, several of them alter behaviour the application code
depends on, and one of them (`0006_tenant_rls`) can make a correctly-migrated database return
nothing at all to an image that was not configured for it. Plan the pair together, roll them back
together.

## Before you start

Three facts decide the whole plan. Get them first.

```bash
# 1. Where the database actually is, and which revision it is on.
uv run alembic current                     # or: psql "$FELIX_DATABASE_URL" -c 'table alembic_version'

# 2. What the application connects as. This is the one that surprises people — see RLS below.
psql "$FELIX_DATABASE_URL" -tAc \
  "select current_user, rolsuper, rolbypassrls from pg_roles where rolname = current_user"

# 3. That a restore exists and you have tested restoring it. A migration is not reversible in the
#    way a deploy is; several downgrades drop columns, and a dropped column is gone.
```

Then confirm the target: `git log --oneline v<current>..v<target>`, and read the `CHANGELOG.md`
entries between them. Anything under **Removed** or **Changed** is where an upgrade breaks.

---

## v0.1.0 → v0.2.0

Five migrations apply: `0005_session_fts`, `0006_tenant_rls`, `0007_approval_consumed_at`,
`0008_fiber_leases`, `0009_memory_recall`.

`0001_baseline` and `0002_a2a_tasks` also differ between the tags. Both diffs are **line-wrapping
only** — semantically identical — so a database already carrying them needs nothing. Verify rather
than trust that: `git diff v0.1.0..v0.2.0 -- migrations/versions/0001_baseline.py`.

### `0006_tenant_rls`, and which image you land on

`0006` applies `ENABLE` **and** `FORCE ROW LEVEL SECURITY` unconditionally to all 16 tenant tables,
whatever `FELIX_DATABASE_RLS` says — the flag is the runtime half, not a gate on the DDL:

```sql
ALTER TABLE "<t>" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "<t>" FORCE  ROW LEVEL SECURITY;
CREATE POLICY felix_tenant_isolation ON "<t>"
  USING (current_setting('app.rls_bypass', true) = 'on'
         OR tenant_id = current_setting('app.tenant_id', true));
```

**In `v0.2.0` exactly, that combination blacks out the deployment.** With the flag false — the
default — the application set neither GUC, so `tenant_id = current_setting('app.tenant_id', true)`
was `NULL`, which is not true, and every one of those tables returned zero rows and rejected writes.
No error; just empty results. Fixed after the tag: the listener now declares `app.rls_bypass` when
RLS is off, so a migrated database is usable without opting in.

So the plan depends on the image you are landing on, and on **fact 2** — the connecting role, since
only a superuser or `BYPASSRLS` role escapes a `FORCE`d policy:

| Target image | Superuser / `BYPASSRLS` role | Plain role that owns the tables |
|---|---|---|
| `v0.2.0` exactly | Fine. RLS is skipped entirely, `FORCE` included. | **Total, silent outage.** Set `FELIX_DATABASE_RLS=true` in the same change as the migration, or land a build that includes the fix. |
| after `v0.2.0` | Fine. | Fine — the bypass is declared for you. |

The right-hand column is the normal case on managed Postgres: neither RDS's master user nor Cloud
SQL's `postgres` is a real superuser. Do not generalise from local Docker — the bundled compose role
is `rolsuper=t, rolbypassrls=t`, which is exactly the configuration that hides all of this.

Measured against a migrated database, reading `thread_state` as a plain role:

| GUC state | Rows visible |
|---|---|
| none — `v0.2.0` with the flag false | **0** |
| `app.rls_bypass=on` — after the fix | 25 |
| `app.tenant_id` set — `FELIX_DATABASE_RLS=true` | 25 |

You cannot skip `0006`: the chain is linear, so `0007`–`0009` require it.

#### If you are turning RLS on

Setting `FELIX_DATABASE_RLS=true` is necessary but not sufficient — **on a superuser or `BYPASSRLS`
connection the policies are skipped and isolate nothing**, while everything appears to work. That is
the failure worth checking for deliberately, because unlike a blackout it is silent in the direction
that matters. `felix doctor` reports it:

```
ok    tenant RLS — enforced
FAIL  tenant RLS — policies active but this role is superuser/BYPASSRLS, which skips them entirely
FAIL  tenant RLS — FELIX_DATABASE_RLS=true but no policies — run `felix migrate head`
```

One further edge: a transaction whose tenant cannot be resolved is left filtered, which is the safe
answer — a bypass there would be a hole — and after the fix it logs at WARNING rather than silently
returning nothing. Background paths that legitimately cross tenants (the fiber scheduler, memory
maintenance) already wrap themselves in `rls_bypass()`; anything you add that queries outside a
request context needs the same.

### Lock profile of the rest

| Migration | What it does | Lock |
|---|---|---|
| `0005_session_fts` | 2× `ALTER TABLE session_events`, one `CREATE INDEX` | **Not `CONCURRENTLY`.** `session_events` is the transcript log and usually the largest table; the build holds a lock that blocks writes for its duration. Size it first: `select pg_size_pretty(pg_total_relation_size('session_events'))`. |
| `0006_tenant_rls` | RLS + policies | Brief `ACCESS EXCLUSIVE` per table. Fast; the risk is behavioural, not lock duration. |
| `0007_approval_consumed_at` | add/drop column, one index | Metadata-only, fast. |
| `0008_fiber_leases` | 3 add, 3 drop, one index | Metadata-only, fast. |
| `0009_memory_recall` | 9 `ADD COLUMN`, 5 indexes, drops a `NOT NULL` | Looks heavy; should be trivial. Its docstring records that `memory_vectors.embedding` was `NOT NULL` with no default while `put_memory` never supplied one, so *every insert has failed on real Postgres since `0001`*. Confirm with `select count(*) from memory_vectors` — expect `0`, and if it is not `0`, re-cost this row before proceeding. |

If `session_events` is large enough that the `0005` index build is not acceptable as downtime,
build it by hand `CONCURRENTLY` first and then `alembic stamp` past it — but that is a deliberate
divergence, so record it somewhere the next upgrade will find.

### New settings

All default safely; none is required.

| Setting | Default | Note |
|---|---|---|
| `FELIX_DATABASE_RLS` | `false` | **Read the RLS section above before accepting the default.** |
| `FELIX_MEMORY_EMBEDDER` | `none` | Long-term memory works without it — recall falls back to full-text. Set it only if you want vector recall, and match `FELIX_MEMORY_EMBEDDING_DIM` to the column, which `0001` fixed at 768. |
| `FELIX_MEMORY_RECALL_LIMIT` | `8` | |
| `FELIX_DEFAULT_MODEL_ID` | `claude-sonnet` | |
| `FELIX_RATE_LIMIT` / `_WINDOW_SECONDS` | `120` / `60` | Now enforced. Confirm it is above your real traffic before it becomes a self-inflicted outage. |
| `FELIX_MCP_STDIO_ALLOWED_COMMANDS`, `FELIX_SANDBOX_ALLOWED_IMAGES`, `FELIX_TRUSTED_CLIENT_IP_HEADER`, `FELIX_ALLOWED_TENANTS` | empty | Allowlists. Empty is the closed default. |

## The sequence

```bash
# 0. Take a backup and verify you can restore it. Not a snapshot you have never restored.

# 1. Build and push the image for the target tag. Nothing does this on a tag.
git checkout v0.2.0
docker build -f deploy/docker/Dockerfile -t <registry>/felix:v0.2.0 .
docker push <registry>/felix:v0.2.0

# 2. Apply migrations, from the target version's code, against the production database.
#    `felix migrate` only ever upgrades — there is no downgrade subcommand.
FELIX_DATABASE_URL=<prod> uv run felix migrate head

# 3. Roll the image, with any settings the table above says you need, in the SAME change.
#    FELIX_DATABASE_RLS in particular must not lag behind the migration.
```

Steps 2 and 3 are one change. If your platform cannot do them atomically, prefer migrating
*immediately* before the roll and keep the gap short — the window between them is the window in
which a plain-role deployment is returning nothing.

## Verify — actually check

```bash
curl -sS https://api.felix.run/health
curl -sS https://api.felix.run/openapi.json | jq '.info.version, (.paths | length)'   # 0.2.0, 68
```

Then the checks that would catch an RLS blackout, which `/health` will not:

```bash
# Reads that must return rows, not empty arrays.
curl -sS -H "authorization: Bearer $KEY" https://api.felix.run/chat/sessions | jq '.sessions | length'
curl -sS -H "authorization: Bearer $KEY" https://api.felix.run/manifests    | jq '.items  | length'
```

An empty array from both, on a deployment that had data, **is** the RLS failure — not an empty
database. Check `pg_roles` for the connecting role before concluding anything else.

Then let the scheduled smoke suite run: `.github/workflows/smoke.yml` exercises health, a sync
`/chat`, a durable `202`, and the thinking/lease/search/abort surfaces against `api.felix.run`. It
does not block PR CI, so a failure there is easy to miss — go and look at it.

## Rollback, and what it cannot undo

Rolling the image back is easy. Rolling the schema back is not, and the two are coupled.

- **Image only.** Safe *only* while the older image tolerates the newer schema. For v0.1.0 against a
  v0.2.0 database this holds — except that a v0.1.0 image never sets the RLS GUCs, so on a plain
  role it lands in the blackout described above. Superuser: fine. Plain role: not.
- **Schema.** Every one of `0005`–`0009` defines a `downgrade()`, so `alembic downgrade 0004` is
  available — via alembic directly, since the `felix migrate` CLI only calls `command.upgrade`.
  Several of those downgrades drop columns, which discards whatever was written into them.
- **Restore.** The only option that undoes data loss, and the reason step 0 is step 0.

A frontend rollback is independent: `felix-web`'s chat-ui versions separately and its current build
is backward-compatible with v0.1.0 — the memory panel reports the harness is too old, and stream
reattach falls back to not reattaching. Neither needs a redeploy when the harness moves.
