# Backup and restore

What to back up, how, and — the part that matters — how to prove the copy restores. A
backup nobody has restored is a hypothesis.

## What holds state

| Store | Holds | Loss means |
|---|---|---|
| **Postgres** (`postgres-data` volume, or your managed instance) | Everything of record: session event logs, manifests and their versions, audit and usage rows, memory vectors, fibers, approvals, jobs, A2A tasks | The deployment's history and every in-flight durable run |
| **Object store** (`felix-data` volume for `FELIX_OBJECT_STORE=fs`; the bucket for `s3`/`gcs`; `minio-data` under `make up-full`) | Bundles, artifacts spilled from large tool outputs, session exports, uploaded documents | Attachments and artifacts referenced from Postgres rows go 404 |
| **Valkey** (`valkey-data`) | Rate-limit windows, the Taskiq queue, cross-process waiters | Nothing durable: a restart empties it and the harness carries on |
| **Warehouse** (`FELIX_WAREHOUSE`, optional) | Append-only spill of audit and usage rows, written *after* Postgres | Analytics history only; Postgres is the system of record |

Back up Postgres and the object store together, from the same moment, so an artifact row
never points at an object the copy does not have. Valkey is deliberately not backed up.

## Postgres

```bash
set -euo pipefail
umask 077                                   # the dump is the database; nobody else reads it
stamp=$(date -u +%Y%m%dT%H%M%SZ)

# Compose. `-Fc` is the custom format: compressed, and pg_restore can pick tables from it.
# Write to a temp name and rename on success, so a failed dump never leaves a file that
# looks like a backup.
docker compose -f deploy/docker/compose.yml exec -T postgres \
  pg_dump -U felix -d felix -Fc --no-owner > "felix-$stamp.dump.part" \
  && mv "felix-$stamp.dump.part" "felix-$stamp.dump"

# A managed instance, from any host that can reach it. Do NOT pass FELIX_DATABASE_URL:
# it is SQLAlchemy-shaped (`postgresql+psycopg://`), libpq does not recognise the driver
# prefix and silently treats the whole string as a database name — the host, user and
# password are discarded. Put the credential in ~/.pgpass (mode 0600), never on the
# command line, where it is visible in `ps` and shell history.
PGHOST=db.example.com PGUSER=felix PGDATABASE=felix \
  pg_dump -Fc --no-owner > "felix-$stamp.dump.part" && mv "felix-$stamp.dump.part" "felix-$stamp.dump"
```

`pg_dump` takes a consistent snapshot; it does not need the API or worker stopped.
Extensions the schema depends on (`vector`) are recorded in the dump and restored with it.

Restore into an **empty** database, then let Alembic confirm the schema is what the code
expects:

```bash
createdb -U felix felix_restore
pg_restore -U felix -d felix_restore --no-owner --exit-on-error felix-<stamp>.dump
# doctor takes the SQLAlchemy-shaped URL; keep it in an env file, not on the command line.
set -a; . ./.env.restore; set +a          # FELIX_DATABASE_URL=postgresql+psycopg://…/felix_restore
uv run felix doctor                        # "ok migrations at head"
```

If doctor reports the schema behind the code, the dump predates a release; run
`felix migrate head` against the restored database before pointing the harness at it.

## Object store

```bash
# fs (the default): the volume is a directory.
docker run --rm -v felix_felix-data:/data -v "$PWD":/backup alpine \
  tar czf /backup/felix-data-$(date -u +%Y%m%dT%H%M%SZ).tgz -C /data .

# s3 / MinIO: mirror the bucket. Versioning on the bucket is the cheaper daily backup.
aws s3 sync s3://felix-bundles ./felix-bundles-$(date -u +%Y%m%d)      # AWS
mc mirror local/felix-bundles ./felix-bundles-$(date -u +%Y%m%d)       # MinIO

# gcs
gsutil -m rsync -r gs://felix-bundles ./felix-bundles-$(date -u +%Y%m%d)
```

Restore is the same command reversed (`tar xzf` into the volume, `aws s3 sync ./copy s3://…`).

## Handling the copies

A dump *is* the database. `session_events` carries every conversation and tool argument,
`audit_events` every governed action, `memory_vectors` what memory capture extracted — so
the file needs the same classification, access control and encryption at rest as the
database it came from, wherever the deployment's data is regulated. Concretely:

- Write dumps with `umask 077` and store them encrypted, in the same class of bucket or
  volume as the data, never on a shared workstation.
- Keeping dumps past `FELIX_*_RETENTION_DAYS` (below) deliberately extends the data's
  lifetime; bound the copies' retention too, and delete them the same way.
- A restore target — the drill stack included — holds production data from the moment
  the restore finishes. Bind it the way production is bound and delete its volumes when
  the drill is over (`docker compose -p felix-drill down -v`).

## The restore drill

Do this once when you set the deployment up, and again after any change to the backup
job. It is the only evidence that the backup works.

1. Take a backup of both stores.
2. Bring up a scratch stack — a second Compose project name, or a namespace — with an
   empty Postgres and an empty object store: `COMPOSE_PROJECT_NAME=felix-drill make up`.
3. Restore both copies into it.
4. `felix doctor` says `ok migrations at head`; `GET /manifests/<name>` returns a stored
   manifest; a thread from the backup replays (`GET /chat/<thread_id>/events`); an artifact
   URL from a spilled tool output resolves.
5. Tear the scratch stack down. Note the wall-clock time the restore took: that is your
   recovery time, and it is what decides how often to take the backup.

## Retention and the backup

The nightly retention sweep deletes rows past `FELIX_*_RETENTION_DAYS`. A backup taken
before the sweep is the only copy of what it removed; if a compliance window is longer
than the retention window, keep the dumps for the difference.

## Upgrades

[`UPGRADING.md`](UPGRADING.md) step 0 is "take a backup and verify you can restore it".
The migrations a release ships are forward-only in practice; the dump from before the
upgrade is the rollback.
