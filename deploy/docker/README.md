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
| `compose.observability.yml` | OTel Collector, Prometheus, Grafana, Jaeger, Loki, Postgres/Valkey exporters |
| `compose.temporal.yml` | Temporal server + UI + `felix-temporal-worker`, on the existing Postgres |
| `compose.memoturn.yml` | Memoturn LLM observability, on the existing Postgres/Valkey/MinIO |
| `config/` | Config mounted read-only by the overlays above |

## Connection pooling

`make up-pooled` puts PgBouncer in transaction mode between Felix and Postgres.

Verified end to end: api, worker, and scheduler each holding their own pool shared **two**
Postgres backends, and 40 consecutive requests — well past the five executions at which
psycopg3 starts preparing statements — went through without a prepared-statement error.
The `migrate` service in the base file runs `felix migrate head` against Postgres directly,
not through the pooler, and the app services wait for it to complete; the overlay leaves
that service alone on purpose.

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

## Observability

`make up-observability` adds metrics, traces and logs to the base stack. It also builds
the image with the `otel` extra — the lean default does not include it, and without it the
API logs `FELIX_OTEL_ENABLED=true but otel extra is not installed` and exports nothing.

| | |
|---|---|
| Grafana | <http://localhost:3000> — provisioned datasources, one `Felix — harness overview` dashboard |
| Prometheus | <http://localhost:9090> |
| Jaeger | <http://localhost:16686> |

Budget roughly 1.7 GiB of additional `mem_limit` on top of the base stack, so this does
not belong on the same host as `make up-lite`.

### The scrape credential

`/metrics` requires authentication, and Compose defaults to `FELIX_AUTH_MODE=api_key`.
`scripts/metrics-token.sh` (run automatically by `make up-observability`) mints a
**second** API key with an empty `scopes` array, merges it into `FELIX_AUTH_API_KEYS`
alongside your operator key, and writes the bare token to `deploy/docker/.metrics-token`
(gitignored, mode 600) because Prometheus has no env expansion in scrape configs.

`/metrics` has no scope gate — any valid key is accepted — so the unscoped key reads
metrics and is refused everywhere else. It is not the operator key, so leaking it does not
leak write access to `PUT /manifests`.

### What is deliberately not here

- **The worker's metrics port is not published.** The worker has no auth middleware, so
  `FELIX_METRICS_PORT` is unauthenticated while carrying the same tenant-supplied label
  values as `/metrics`. Prometheus reaches it over the Compose network; nothing else should.
- **Logs arrive over OTLP, not by tailing `/var/lib/docker/containers`.** File tailing
  needs the collector to run as root to read `root:root 0640` files, gives it every
  container's logs, fails silently when it cannot, and does not survive Kubernetes.
  Emitting from the process also stamps `trace_id`/`span_id` on each record, so a log line
  links to the span that produced it instead of being matched by a regex.
- **Optional overlays are discovered from files.** `config/prometheus-targets/` is empty by
  default; an overlay drops a target file in. A static entry for a service that is usually
  not running is a permanently-down target, which teaches operators to ignore a red target list.

See [docs/OBSERVABILITY.md](../../docs/OBSERVABILITY.md) for the metric catalog and span schema.

## Temporal

`make up-temporal` swaps durable execution from the Postgres fiber sweeper to Temporal, and
brings up the server, its UI on <http://localhost:8233>, and `felix-temporal-worker` on task
queue `felix-fibers`.

It reuses the existing Postgres rather than standing up a second one: `auto-setup` creates
`temporal` and `temporal_visibility` inside it on first boot, which keeps the overlay within
reach of a small VM. `auto-setup` is a development convenience and is not a production shape.

`FELIX_DURABILITY=temporal` is set in the overlay for **every** process that has to agree
about it — api, worker and the temporal-worker. They must agree, or a durable chat is
enqueued for one driver and executed by the other. The image is built with the `temporal`
extra there too, since `durability/temporal.py` raises without `temporalio` rather than
degrading — which is correct: a durable chat that quietly ran transiently would be worse.

Known gap, unchanged by this overlay: `Client.connect` takes no TLS or API-key options, so
**Temporal Cloud is not reachable** as configured. `docs/ROADMAP.md` carries the open
"Temporal: decide" item.

To scrape Temporal's own metrics alongside the observability overlay, add a target file —
see `config/prometheus-targets/README.md`.

## Memoturn

`make up-memoturn` runs [Memoturn](https://memoturn.com) locally and points Felix's OTLP
export at it. Console on <http://localhost:3002>, API on :3001.

Felix needs **no Memoturn SDK and no new dependency**. Memoturn ingests OTLP natively and
maps spans carrying `gen_ai.*` semantic-convention attributes to generations — which is
what `felix.patterns.model` already emits — so the whole integration is an endpoint and a
header, expressed as `FELIX_OTEL_PROTOCOL=http` and `FELIX_OTEL_HEADERS`. That is the
"Protocols, not vendors" rule applied to telemetry: nothing in Felix knows this vendor exists.

### First run

The overlay needs three secrets in `.env` before it will start (Compose fails with the
reason rather than booting a broken container):

```bash
MEMOTURN_AUTH_SECRET=$(openssl rand -base64 48)
MEMOTURN_ENCRYPTION_KEY=$(openssl rand -base64 48)
MINIO_ROOT_PASSWORD=$(openssl rand -hex 32)
```

Then bring it up, create a project in the console, and put its keys in `.env` as a single
base64 blob — the shape Memoturn's `Authorization: Basic` header wants:

```bash
FELIX_MEMOTURN_BASIC_AUTH=$(printf '%s:%s' "$PUBLIC_KEY" "$SECRET_KEY" | base64)
```

Restart `api` and `worker` to pick it up. A chat then appears in the console as a
**generation** with model, token usage and cost — not a bare span. That is the check that
the `gen_ai.*` attribute names are right.

### What it reuses

Memoturn's own compose stands up Postgres, Valkey, MinIO, Caddy and Apache Doris — a 4 GB
floor, more than the whole Felix stack. Here it borrows Felix's:

| Memoturn needs | Uses Felix's |
|---|---|
| Postgres | the same container, its own `memoturn` database, created by a one-shot idempotent job |
| Redis | the same Valkey, on a separate database index |
| Blob storage | the same MinIO (hence `--profile full`), its own bucket |
| Telemetry store | `TELEMETRY_ENGINE=postgres`, which drops Doris entirely |
| TLS terminator | none — Caddy is for production; this publishes on `127.0.0.1` |

The database and the bucket are both created by one-shot idempotent jobs rather than by a
`/docker-entrypoint-initdb.d/` script, because that only runs on a *fresh* Postgres volume
and would silently do nothing for anyone who already has one. Without the bucket, ingest
answers `500 NoSuchBucket`; without the database, nothing starts at all.

The console serves the SPA **and** proxies `/api/*` and `/auth/*` to the API from its own
Caddy, using `config/memoturn-console.Caddyfile`. The console bundle is built with
`VITE_API_BASE=/api` and Memoturn's production front proxy is what routes those; this
overlay has no front proxy, because TLS is the only other thing that one does. Without the
rules every SPA call 404s on the static server and the page reads "Something went wrong"
while the API is perfectly healthy. Same-origin on purpose — pointing the SPA at `:3001`
directly would pull in CORS and SameSite cookie rules for the session.

**One caveat of sharing Valkey.** Felix runs it with `--maxmemory-policy allkeys-lru` and
Memoturn's queue (BullMQ) wants `noeviction`, so under memory pressure a queued ingest job
can be evicted and that telemetry is lost without an error. Fine locally; give Memoturn its
own Valkey if you run this for anything you need to keep.

### Sending to Memoturn and Jaeger at once

A process has one OTLP destination, so this overlay points Felix straight at Memoturn. To
fan out, run the observability overlay too, leave `FELIX_OTEL_ENDPOINT` on the collector,
and add an `otlphttp` exporter for Memoturn to `config/otel-collector.yaml` — the collector
is the component whose job that is. Note the collector validates every *defined* exporter,
so only add it once the credentials are set.
