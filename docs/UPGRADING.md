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

## Thread previews for threads from before the session index listed them

**One command after the roll, optional, and safe to repeat.** `GET /chat/sessions` lists each thread
with `preview` (its first user message, masked and cut to 120 characters) and `manifest`. A turn
records the preview, so a thread that has not had one since the upgrade lists `preview: null`, and
a client shows it by id. To fill those from each thread's session log, once:

```bash
docker compose exec api felix sessions backfill-previews --dry-run   # counts per tenant, writes nothing
docker compose exec api felix sessions backfill-previews             # every tenant; --tenant <id> for one
```

Run it in the `api` container: the preview is masked with the secret values that process can see,
the same rule a turn applies. It never overwrites a preview, leaves `updated_at` alone (so the thread
list keeps its order, and retention's idle clock does not restart), and creates no thread. A thread
with no user text in its log stays null. Each thread is one read of the start of its log and, if it
is filled, one short write locked to that thread's row, 200 threads to a page (`--batch-size`). So it
can run with the API serving. A thread that fails is named and the command exits 1; running it again
retries exactly the threads still missing a preview.

`manifest` needs no backfill: it falls back to the pin's `manifest_name`, which every thread that has
had a turn on 0.2.0 or later carries. The session log does not record which manifest a turn ran under, so
the thread's latest manifest is not recovered for a thread that has not had a turn since the upgrade.

## Hosted workspaces (opt-in)

**Nothing changes unless you set `FELIX_WORKSPACE_BACKEND=hosted`.** With it, each thread's or
tenant's workspace lives in its own sandbox: a Cloudflare Container started with the internet off,
reached through the gateway Worker in `deploy/cloudflare/workspace-gateway`, with `/workspace` kept
in an R2 bucket between runs. The workspace tools behave exactly as on `local`; a conformance test
holds the two to the same answers. To turn it on:

1. Deploy the gateway (its `README.md`): create the `felix-workspaces` R2 bucket, set
   `WORKSPACE_GATEWAY_TOKEN` (32+ characters), `wrangler deploy`.
2. On the API and the worker, set `FELIX_WORKSPACE_BACKEND=hosted`,
   `FELIX_WORKSPACE_GATEWAY_URL` (the Worker's https URL) and `FELIX_WORKSPACE_GATEWAY_TOKEN`
   (the same token). Boot refuses `hosted` without both.

`shell_tools` run in the scope's sandbox as well, after the same allowlist and command screening as
on the host. The sandbox image is a slim Debian with Python and little else, so a command that is
not installed there fails as a missing binary. A repository opened in a thread is cloned into that
thread's sandbox, and `publish_commits` reads its commits there; the thread's workspace must be
empty when the repository is opened. A checkout made on the host before `hosted` was turned on is
still refused: remove it and open the repository again. What `hosted` does not serve, and refuses
rather than serving from the host: an image tool's `path`, and `publish_commits` in a thread with
no repository, for any scope but `deployment` (which stays on the host).

A scope starts empty on the hosted backend. To carry what local scopes already hold, once, after
turning `hosted` on (the local files stay where they are):

```bash
docker compose exec api felix workspace upload --dry-run
docker compose exec api felix workspace upload
```

## Workspaces are per thread by default

**One command after the roll, and only if agents had already written files.** Every workspace tool
(`list_dir`, `read_file`, `write_file`, `edit_file`, `search_files`), `shell`, the image tools'
`path` and `publish_commits` used to work in the one directory `FELIX_WORKSPACE_ROOT` names, for
every tenant and thread on the host. Each call now works in its manifest's scope
(`spec.workspace.scope`, [`WORKSPACE.md`](WORKSPACE.md)):

| `scope` | Directory | Default for |
|---|---|---|
| `thread` | `<root>/.felix-scopes/<tenant>/<hash>` | every manifest that does not say |
| `tenant` | `<root>/.felix-scopes/<tenant>/shared` | — |
| `deployment` | `<root>` itself | `contributor`, `triage` |

`deployment` holds every other scope, so it is honoured only for the tenants in
`FELIX_WORKSPACE_DEPLOYMENT_TENANTS` (default `default`); any other tenant running a manifest that
asks for it is refused. A call with no thread under `scope: thread` (an MCP or A2A call that names
none) is refused too, rather than given a shared directory.

**Files already at the root are no longer what an agent sees**, except under `deployment`. Move them
into the `default` tenant's `shared` scope, once, where the workspace is mounted:

```bash
docker compose exec api felix workspace migrate --dry-run   # what would move
docker compose exec api felix workspace migrate --keep AGENTS.md
```

`--keep` leaves a name at the root: instruction files (`AGENTS.md`, `SYSTEM.md`) are read from the
root when `FELIX_LOAD_AGENTS_MD` is on, and only the operator's `deployment` scope can write there
now. Nothing is overwritten and a second run moves nothing. A manifest that should see those files
declares `workspace: {scope: tenant}`; a new thread under the default scope starts empty.

`felix-shell-runner` must run the same release as the API: the API now tells it which scope a
command runs in, and an older runner ignores the field and runs at the root.

## The Temporal backend was removed

**Only matters if you set `FELIX_DURABILITY=temporal`.** It drove fibers through Temporal but used
none of Temporal's durability features, so it gave Felix's guarantees, not Temporal's. Fibers are
now the one durable path.

- **Unset `FELIX_DURABILITY`** (or set it to `fibers`). With `temporal`, every Felix process now
  refuses to start, and the error says this.
- **Stop `felix-temporal-worker`.** The console script, `felix temporal-worker`, the `temporal`
  extra and `deploy/docker/compose.temporal.yml` (`make up-temporal`) are gone.
- **In-flight runs carry on.** A durable chat Temporal had started is a fiber row with
  `backend: temporal`, and its state is all in Postgres. The fiber scheduler in `felix-worker` now
  claims those rows, which it skipped while Temporal existed. Nothing needs migrating, but keep the
  Temporal worker stopped before upgrading, so the two never drive the same row. A row older than
  its run's TTL (at most a day) is marked `expired` rather than run; if it named webhooks, they
  fire once with that `expired` status.
- `FELIX_TEMPORAL_HOST` and `FELIX_TEMPORAL_NAMESPACE` are now ignored; remove them when convenient.
  The Temporal server's own databases (`temporal`, `temporal_visibility`, if you used the overlay's
  auto-setup) are yours to drop.

---

## `0034_fiber_thread`: one durable run per thread

**A catalog-only column, a small index, and a refusal clients may not have seen before.**
`fibers.thread_id` records the thread a durable chat writes to, backfilled from `state_json` for
every existing `durable_chat` row (under the RLS bypass, as `0029` does), with a partial index over
runs that can still be in flight. Once the new image serves, a send to a thread whose durable run
is in flight -- `POST /chat` or `POST /chat/stream`, durable or not -- is refused with
`409 run_in_progress:<resume_token>` instead of starting a second run beside it, and
`GET /chat/sessions/{id}` names the run as `activeRun`.

- **Clients.** A client that treated every 409 from `/chat` as a lease refusal should read the
  `detail` code. A resend under the `Idempotency-Key` of the message that started the run is
  still answered from its key, not refused.
- **During the roll.** Old replicas create fibers without `thread_id`, so a send landing on an old
  replica is not refused, and a run an old replica starts is invisible to new ones' check until
  it ends. Roll quickly; nothing needs re-running afterwards.
- **A thread is never held for good.** A run past its `expires_at` that no worker holds does not
  count, so a deployment with no worker cannot lock a thread with a fiber nothing will claim.
- **Rollback.** The downgrade drops the index and the column; old code never reads either.

## `0033_skill_owner` re-keys the skill library

**Brief locks on three small tables, failed skill saves until the roll completes, and a rollback
past it can refuse.**

`skill`, `skill_version` and `skill_file` gain an `owner` column (`''` for every existing row,
which stays the tenant's own library) and their primary keys gain it beside the name, which is
what lets a personal skill share a name with an org one. Adding the column is catalog-only; each
primary key index is rebuilt under an ACCESS EXCLUSIVE lock, so skill saves and catalog loads
(every agent compile that reads the library) wait for the build. The tables hold one row per
skill, version and file, so the wait is short on any deployment this repo has seen.

Code from before this revision saves a skill with an upsert on `(tenant_id, name)`, which is no
longer a unique key. Once the migration has run, every skill save — an agent's draft, an
operator's edit, an import — fails on a replica still running the old code, until that replica is
replaced. Reads are unaffected. Helm migrates in a pre-upgrade hook while old pods still serve, so
roll quickly, or hold skill writes for the length of the rollout.

The downgrade refuses while any of the three tables holds a personal row (`owner <> ''`): the old
key cannot hold two owners' skills of one name, and a personal skill that did fit would silently
become the org's. Remove the rows from all three tables, or stay on the newer code:

```bash
psql "$FELIX_DATABASE_URL" -c "set app.rls_bypass = 'on'" -c "
  select 'skill' as t, count(*) from skill where owner <> ''
  union all select 'skill_version', count(*) from skill_version where owner <> ''
  union all select 'skill_file', count(*) from skill_file where owner <> ''"
```

`app.rls_bypass` is needed on managed Postgres, where the forced tenant policy binds the table
owner and a plain count reads zero. A downgrade leaves the personal skills' files in the object
store under `skill-library/{tenant}/~…/`; nothing reads them afterwards, so delete that prefix if
you want them gone.

---

## `0020_ordering_indexes` locks tables while it builds

**Six indexes rebuilt, one added — plan a quiet window on a large deployment.**

The listings now order down to a unique key (timestamp, then id `COLLATE "C"`), and 0020 builds
indexes that match those orderings exactly so no listing sorts: `audit_events`, `usage_events`,
`approvals`, `plans`, `memory_vectors` and `job_runs` each get one, five of them replacing an
index that is a prefix of the new one, and `fibers` gains a partial index for the worker's claim.
Like every migration here it uses plain `CREATE INDEX`, which holds a write lock on the table
for the length of the build. `audit_events` and `usage_events` are the ones that grow without
bound; on those, time the build before you commit to a window:

```bash
psql "$FELIX_DATABASE_URL" -c "select relname, n_live_tup from pg_stat_user_tables \
  where relname in ('audit_events','usage_events','memory_vectors','job_runs','fibers')"
```

The downgrade restores the previous indexes. Reads keep working during the build — only writes
to the table being indexed wait, and the audit and usage writers buffer and retry.

---

## Tenant ids are held to a charset

**No migration, and it can still lock people out — check before you deploy.**

`assert_valid_tenant_id` used to reject only `:`, `#` and surrounding whitespace. It now
requires the whole id to match `[A-Za-z0-9._-]+`, not be `.` or `..`, and stay within 128
characters — the same rule `storage/fs.py` applies to an object-key segment, because a
tenant id is one in four key spaces.

`tenant_id` columns are `Text`, so a database can hold ids the new rule refuses: an email
address, anything with a space or an uppercase-plus-punctuation shape, non-ASCII, or
whatever a permissive development deployment minted. After the upgrade those principals are
refused at authentication, and **their rows stay in the database addressable only by a
principal that can no longer exist**. That is an availability incident wearing a security
fix's clothes, so find out first:

```sql
-- Any tenant id the new rule refuses. Run against the deployment's own database.
SELECT DISTINCT tenant_id FROM audit_events
WHERE tenant_id !~ '^[A-Za-z0-9._-]{1,128}$' OR tenant_id IN ('.', '..');
```

`audit_events` sees every tenant that has done anything; widen to `sessions`, `manifests`
and `memory_facts` if you want certainty. An empty result means this section does not apply
to you — which is the expected answer for a deployment whose tenants are slugs, UUIDs or
domains.

If it is not empty, decide before upgrading rather than after: rename the tenant in place
(every table carrying `tenant_id`, in one transaction, with the deployment stopped), or stay
on the previous image until you can. There is no compatibility flag — the rule is a door,
and a door that can be turned off for some callers is not one.

Configuration is checked too, and that failure is loud: a tenant id pinned in
`FELIX_JWT_VERIFIERS` (`;tenant=fixed:…`), `FELIX_ALLOWED_TENANTS` or `FELIX_AUTH_API_KEYS`
that does not match the rule now refuses to start, naming the setting. Before this it
started and returned `401` to every request from that issuer with nothing in the log.

## The workspace is a named volume, not the checkout

**No migration, and it can make an agent's files look gone — check before you deploy.**

Compose used to mount `${FELIX_WORKSPACE_HOST:-./workspace}` at `/workspace`, so a deployment
that never set `FELIX_WORKSPACE_HOST` had every file an agent wrote inside its own git checkout,
beside the compose files and `.env`. The default is now a named volume, `felix-workspace`, and the
image creates `/workspace` owned by its runtime user (uid `10001`) so a new volume is writable from
the first call. `scripts/check-compose-render.py` fails any render that bind-mounts a host directory
there without `FELIX_WORKSPACE_HOST` set. [`WORKSPACE.md`](WORKSPACE.md) is the design this starts.

**If you set `FELIX_WORKSPACE_HOST`, nothing changes.** It still overrides the default with a host
path; the directory must be writable by uid `10001`:

```bash
sudo chown -R 10001:10001 "$FELIX_WORKSPACE_HOST"
```

**If you did not, the workspace starts empty after the upgrade**, because the new volume is not the
old directory. Either keep the old location, explicitly:

```bash
echo 'FELIX_WORKSPACE_HOST=./workspace' >> .env
sudo chown -R 10001:10001 ./workspace      # the published image cannot write a host-owned dir
```

or copy what is there into the volume once, after the first `up` has created it:

```bash
docker run --rm -v "$PWD/workspace:/from:ro" -v felix_felix-workspace:/to alpine \
  sh -c 'cp -a /from/. /to/ && chown -R 10001:10001 /to'
```

The volume is seeded with the image's ownership only when it is first created. A `felix-workspace`
volume that an older image created belongs to root and stays that way; the `chown` in the copy
above, or the same command with only `-v felix_felix-workspace:/to`, fixes it.

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
| `0013_drop_oauth_token_cache` | `DROP TABLE IF EXISTS oauth_token_cache` | The first migration that drops a table. Unconditional and, through `downgrade()`, irreversible for data — the table recreates empty. Safe because no released version ever wrote to it (`select count(*) from oauth_token_cache` before upgrading is `0` on every deployment built from this repo). |

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
#    docs/BACKUP.md has the commands and the drill.

# 1. Move the checkout to the tag (it supplies the migrations and compose files).
cd /opt/felix && git fetch --tags && git checkout vX.Y.Z

# 2. Pin the release. Deployments that use the published image set the tag in the
#    host .env rather than the repo defaulting it.
echo 'FELIX_IMAGE_TAG=X.Y.Z' >> .env     # or edit the existing line

# 3. Roll. The `migrate` service runs `felix migrate head` from the target image and
#    api, worker and scheduler wait for it to complete, so the new app never boots
#    against a schema it does not have yet. `felix migrate` only ever upgrades —
#    there is no downgrade subcommand.
docker compose <-f overlays…> up -d

# To migrate ahead of the roll instead (a long migration you want to watch):
docker compose <-f overlays…> run --rm migrate
```

On a GCE + Compose deployment, `deploy/gcp/roll.sh <version>` runs this whole sequence from your
own machine, with a preflight and a verified backup first, asking before each change (`--check`
for the read-only preflight alone). The questions need a terminal; without one it refuses up
front, and `--yes` answers them all yes — except with durable runs in flight, where it stops.

`make up-gcp` wraps that last command, but **`make` is not installed on every host** — a minimal
VM image often lacks it, and the failure (`make: command not found`) happens before anything rolls.
The compose invocation above is what the target runs and needs no `make`.

The migration and the roll are one `up`. If you split them with the `run --rm migrate` form,
keep the gap short — the window between them is the window in which a plain-role deployment
is returning nothing.

### Helm

`helm upgrade` runs the migrate Job as a pre-upgrade hook, so the order above is built in.
Coming from chart 0.2.2 or earlier: the chart moved from one Deployment running all three
processes to one per process (`<release>-api`, `-worker`, `-scheduler`). A Deployment's
selector is immutable, so the upgrade deletes the old object and creates the new ones, and
the api is briefly absent between them — schedule it like a restart. `replicaCount`,
`resources`, `autoscaling` and `podDisruptionBudget` moved under `api.`; a values file
still setting them at the top level fails the render and says where they went. Details in
[deploy/helm/README.md](../deploy/helm/README.md).

## Verify — actually check

```bash
curl -sS https://api.felix.run/health
curl -sS -H "authorization: Bearer $KEY" https://api.felix.run/openapi.json | jq '.info.version, (.paths | length)'   # 0.2.0, 68
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
- **Schema.** Every one of `0005`–`0013` defines a `downgrade()` (`0013`'s recreates `oauth_token_cache` empty), so `alembic downgrade 0004` is
  available — via alembic directly, since the `felix migrate` CLI only calls `command.upgrade`.
  Several of those downgrades drop columns, which discards whatever was written into them.
- **Restore.** The only option that undoes data loss, and the reason step 0 is step 0.

A frontend rollback is independent: `felix-web`'s chat-ui versions separately and its current build
is backward-compatible with v0.1.0 — the memory panel reports the harness is too old, and stream
reattach falls back to not reattaching. Neither needs a redeploy when the harness moves.
