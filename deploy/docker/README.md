# Felix Docker packaging (image + Compose).
#
# Always run Compose from the **repo root** so `.env`, build context, and
# `./workspace` resolve correctly:
#
#   make up
#   make up-lite
#   make up-gcp    # GCE / public VM (no DB/cache publish)
#   make up-pooled # + PgBouncer, for many workers on few connections
#
# Equivalent:
#   docker compose -f deploy/docker/compose.yml --project-directory . up --build

| File | Role |
|------|------|
| `Dockerfile` | Multi-stage CPython image (context = repo root) |
| `compose.yml` | api, worker, scheduler, Postgres, Valkey (+ MinIO profile) |
| `compose.lite.yml` | Tight mem caps; no host ports for DB/cache |
| `compose.gcp.yml` | Public VM: no DB/cache publish; workspace mount |
| `compose.pgbouncer.yml` | PgBouncer in transaction mode in front of Postgres |

## Connection pooling

`make up-pooled` puts PgBouncer in transaction mode between Felix and Postgres.

Verified end to end: api, worker, and scheduler each holding their own pool shared **two**
Postgres backends, and 40 consecutive requests — well past the five executions at which
psycopg3 starts preparing statements — went through without a prepared-statement error.
Run `felix migrate head` against Postgres directly, not through the pooler; the schema has
to exist before the app can serve anything.

Reach for it when the connection arithmetic stops working. Each Felix process holds its
own pool, so the ceiling is:

```
FELIX_WORKERS x (FELIX_DB_POOL_SIZE + FELIX_DB_MAX_OVERFLOW)
```

Four workers on the defaults is 120 connections against a stock Postgres
`max_connections` of 100 — and raising `max_connections` trades one wall for another,
since every backend costs memory. A transaction-mode pooler multiplexes many cheap
client connections onto few server ones instead.

What changes for Felix: its own pool becomes a pool of *client* connections to
PgBouncer, which are cheap, while `PGBOUNCER_POOL_SIZE` is the real server-side limit.
Raise the former freely; size the latter against your database.

| Variable | Default | Meaning |
|---|---|---|
| `PGBOUNCER_MAX_CLIENT_CONN` | 500 | Client connections accepted — the number that gets to be large |
| `PGBOUNCER_POOL_SIZE` | 25 | Server connections opened — what `max_connections` must accommodate |
| `PGBOUNCER_MAX_PREPARED_STATEMENTS` | 200 | See below |

**The one combination that fails, and fails late.** psycopg3 auto-prepares a statement
after five executions. Under transaction pooling the sixth lands on a different server
connection where that statement was never created, so the failure arrives well after
startup and looks like something else. Either the pooler tracks prepared statements or
the client stops making them — never neither:

- PgBouncer >= 1.21 with `MAX_PREPARED_STATEMENTS > 0` tracks them. This overlay does,
  so `FELIX_DB_PREPARED_STATEMENTS` stays `true`.
- Any pooler that does not — including **RDS Proxy**, which pins the session when it
  sees a prepared statement and thereby defeats the multiplexing you deployed it for —
  needs `FELIX_DB_PREPARED_STATEMENTS=false`.

Run migrations against Postgres directly rather than through the pooler. Nothing breaks
under transaction pooling, but a migration is a one-off admin action and there is no
reason to route it through a multiplexer sized for request traffic.

## Image hardening

The runtime stage applies pending OS security updates and removes `pip`
(including its vendored `msgpack` and `setuptools`, which carry their own
CVEs). The virtualenv is built by uv in the builder stage and copied whole, so
nothing at runtime needs pip.

CI scans the built image with Trivy and fails on fixable HIGH/CRITICAL
findings, so this stays true. Verify locally with:

```bash
docker build -f deploy/docker/Dockerfile --build-arg FELIX_EXTRAS= -t felix:local .
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock aquasec/trivy:latest \
  image --scanners vuln --ignore-unfixed --severity HIGH,CRITICAL felix:local
```

Note the trade-off: base images are digest-pinned for reproducibility, but
`apt-get upgrade` means OS package versions can still move between builds.
Security patching wins over bit-identical rebuilds here.
