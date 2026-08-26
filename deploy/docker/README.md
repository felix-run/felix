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
| `compose.replicas.yml` | Two API replicas behind one nginx origin |

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

## Two replicas behind one origin

```bash
make up-replicas               # boot it
./scripts/smoke-replicas.sh    # boot it, prove it, tear it down
```

Almost everything in Felix behaves identically on one replica and on two, which is
precisely why the parts that do not are the parts nothing exercises by accident:

- a resume stream reattaches to whichever replica the origin picks, which is generally
  not the one that ran the turn. `session/notify.py` carries the wake across — and on a
  single replica the in-process waiter answers first, so Redis is never consulted and a
  completely broken pub/sub fan-out is indistinguishable from a working one.
- steer, approvals and fiber leases are Redis- and Postgres-backed for the same reason.
  A second replica is what proves none of them are quietly process-local.

The smoke script runs the stack with `FELIX_STREAM_RESUME_POLL_SECONDS=30`, and that is
the whole design rather than a detail. At the default 1 s floor a cross-replica append
would reach the reader within a second whether or not it was ever notified, so the test
would pass against a broken notification layer. At 30 s, prompt delivery has only one
explanation. A measured run delivered in **2 ms**:

```
   10 upstream=192.168.32.4:8080
   10 upstream=192.168.32.5:8080
   A is subscribed to felix:thread:default:default:smoke-…
   delivered 2 ms after the append, against a 30s poll floor
```

Verified to fail, too: with the publish in `notify_appended` disabled, the script exits
non-zero with `NOT DELIVERED`.

nginx is a load balancer here and nothing more — not a recommendation about your
production ingress. Two settings in `nginx-replicas.conf` are load-bearing for any
origin you put in front of Felix: `proxy_buffering off`, without which an SSE stream is
withheld until the response completes and looks hung; and a `proxy_read_timeout` longer
than `FELIX_STREAM_RESUME_IDLE_SECONDS`, so the origin is not what closes a healthy
idle stream.

Combine with `compose.pgbouncer.yml` when you want both. Two replicas is also what
makes the connection ceiling arrive sooner, since the pool is per process.
