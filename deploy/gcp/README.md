# Deploy Felix on GCP

Felix is cloud-agnostic. On GCP, map Protocols to managed services:

| Protocol | GCP |
|----------|-----|
| Postgres + pgvector | Cloud SQL Postgres with `pgvector`, or AlloyDB |
| Cache | Memorystore (Redis/Valkey) |
| Object store | GCS — set `FELIX_OBJECT_STORE=gcs`, `FELIX_GCS_BUCKET=…` |
| Secrets | `FELIX_SECRETS_BACKEND=gcp` (Secret Manager) with `felix-harness[gcp]` |
| Compute | GKE (Helm chart in `deploy/helm/felix`) or Cloud Run (API + worker + scheduler) |

## Minimal env

```bash
FELIX_CLOUD_PROVIDER=gcp
FELIX_OBJECT_STORE=gcs
FELIX_GCS_BUCKET=your-bucket
FELIX_GCP_PROJECT=your-project
FELIX_SECRETS_BACKEND=gcp
FELIX_DATABASE_URL=postgresql+psycopg://...
FELIX_REDIS_URL=redis://...
FELIX_AUTH_MODE=jwt
FELIX_CONSUMER_SHARED_SECRET=$(openssl rand -hex 32)  # required for /internal
```

Prefer Workload Identity over JSON key files.
Install extras: `uv sync --extra gcp` (or image build with `FELIX_EXTRAS=gcp`).

The compose overlay deploys the published `-gcp` image rather than building on the host, so set the
release you want in the host `.env` and bring it up:

```bash
echo 'FELIX_IMAGE_TAG=0.18.0' >> .env         # the release to run; required, not defaulted
docker compose -f deploy/docker/compose.yml \
  -f deploy/docker/compose.gcp.yml -f deploy/docker/compose.lite.yml \
  --project-directory . up -d
```

Upgrading is then bumping `FELIX_IMAGE_TAG` and repeating that `up -d`. Note `make` is not
necessarily installed on a minimal VM image — the compose invocation above is what `make up-gcp`
runs, and works without it.

## GCE + Compose

On the VM (`/opt/felix`), set:

```bash
FELIX_SECRETS_BACKEND=gcp
FELIX_GCP_PROJECT=your-project
FELIX_IMAGE_TAG=0.18.0     # the published `-gcp` image already carries the gcp (and, from 0.19.0, otel) extras
# Leave Anthropic empty — hydrate from Secret Manager:
#   secret id: felix-anthropic-api-key  (or ANTHROPIC_API_KEY)
ANTHROPIC_API_KEY=
FELIX_ANTHROPIC_API_KEY=
```

Grant the VM service account `roles/secretmanager.secretAccessor` on that secret,
then:

```bash
make up-gcp
# or:
docker compose -f deploy/docker/compose.yml \
  -f deploy/docker/compose.gcp.yml -f deploy/docker/compose.lite.yml \
  --project-directory . up -d
```

See `deploy/docker/README.md`.

### Upgrading

`deploy/gcp/roll.sh <version>` runs the upgrade in [`docs/UPGRADING.md`](../../docs/UPGRADING.md)
against the VM from your own machine: preflight, a backup it proves can be read back, the
checkout and `FELIX_IMAGE_TAG` pin, a pull, `docker compose up -d`, and a wait for `/health` to
report the version. It asks before each step that changes the host. Run it with `--check` first,
which only reads. Its header lists the variables that point it at your VM.

The questions are read from the terminal, so run it from one. With no terminal — CI, an agent's
shell, Claude Code's `!` prefix — it refuses before doing anything, unless you pass `--yes`, which
answers every confirmation yes and prints each one. `--yes` still stops if durable runs are in
flight: restarting the worker under them is a decision to make interactively.

### Rolling from CI

`roll.sh` is also what a CI job runs: `--yes` answers its confirmations, and two things exist for
a caller with no terminal.

- `FELIX_SSH_FLAGS` is appended to every `gcloud compute ssh`. A runner reaching a VM that takes
  SSH only from IAP passes `--tunnel-through-iap` (and `--ssh-key-expire-after=1h`, so OS Login
  keys do not pile up on the deployer's profile).
- Exit status 75 means *deferred*: durable runs are in flight, `--yes` does not roll over them,
  and nothing was changed. Try again later. Any other non-zero status is a failure.

Before it reports done, the roll waits for `/health` to report the version and then for `/ready`
to answer 200, so a release that boots but cannot reach its database, cache or object store
fails the roll.

A release that needs a person before it rolls (a new setting in the host `.env`, a decision)
carries `<!-- deploy: manual -->` in its `docs/UPGRADING.md` section; an automated caller should
stand down for it, and for any such release between the live version and the target.

The reference deployment (api.felix.run) is rolled this way from a private operations
repository, so production logs and the deployer identity stay out of this public one.

### Telemetry to Google Cloud

[`compose.otel-gcp.yml`](../docker/compose.otel-gcp.yml) adds one OpenTelemetry Collector to the
stack (192 MiB). Felix sends it traces and trace-correlated logs over OTLP, and it scrapes the API's
`/metrics` and the worker's metrics port. Everything goes to Cloud Trace, Cloud Logging and Managed
Service for Prometheus, authenticated by the VM's service account. Nothing stores or shows telemetry
on the VM, so the record survives the VM having a bad day. The local
[`compose.observability.yml`](../docker/compose.observability.yml) (Grafana, Jaeger, Loki) is not a
production shape: its traces live in memory and its Grafana is an anonymous admin.

The `-gcp` image carries the `otel` extra from 0.19.0. To turn it on:

1. Enable the APIs (`gcloud services enable cloudtrace.googleapis.com logging.googleapis.com
   monitoring.googleapis.com`; Cloud Trace is off in a new project), grant the VM's service account
   `roles/cloudtrace.agent`, `roles/logging.logWriter` and `roles/monitoring.metricWriter`, and
   check that the VM's access scopes allow them (`cloud-platform`, or the three APIs).
2. Write `deploy/docker/.metrics-token` (git-ignored): a credential holding only `metrics:read`,
   which `/metrics` requires. Under `FELIX_AUTH_MODE=api_key`, `scripts/metrics-token.sh` mints a
   key. Under `jwt`, which accepts no API key, mint a JWT in the running API container. The file
   must be readable by the collector, which runs as uid 10001, and a failed mint must not leave an
   empty token behind (the scrape would fail quietly):

   ```bash
   cd /opt/felix
   sudo docker compose -p felix exec -T api felix mint-jwt --sub metrics-scrape --tenant default \
     --scopes metrics:read --ttl 7776000 --allow-long-ttl | sudo tee /tmp/metrics-token >/dev/null
   sudo test -s /tmp/metrics-token && sudo install -o 10001 -g 10001 -m 600 /tmp/metrics-token \
     deploy/docker/.metrics-token; sudo rm -f /tmp/metrics-token
   ```

   The token lasts 90 days. Renew it before then, and alert on `up{job="felix-api"} == 0` in Cloud
   Monitoring, which is how an expired one shows.
3. Bring the stack up with the overlay once (`make up-gcp-otel`, or the `docker compose` line above
   with `-f deploy/docker/compose.otel-gcp.yml` added last). `roll.sh` reuses the compose files the
   running stack was started with, so every later roll keeps it. Rolling back to a release older
   than the overlay means bringing the stack up without it first: the old checkout has no
   `compose.otel-gcp.yml`, and `roll.sh` would fail its pull after moving the checkout.

Prompts stay off spans unless `FELIX_OTEL_CAPTURE_CONTENT=true`; the overlay pins the rest of the
transport, so a vendor's OTLP settings left in `.env` do not reach the collector. Managed Prometheus
bills per sample, which is why the collector scrapes once a minute.

### Backups

`roll.sh` dumps only at upgrades, onto the VM's own disk. [`backup/`](backup/README.md) adds
daily disk snapshots, a nightly dump of Postgres and the object store to a GCS bucket the VM
cannot delete from, and a restore drill that proves the newest copy restores.

## Helm

```bash
helm upgrade --install felix ./deploy/helm/felix \
  -f deploy/gcp/values-gke.example.yaml \
  --set secrets.databaseUrl="$FELIX_DATABASE_URL" \
  --set secrets.redisUrl="$FELIX_REDIS_URL" \
  --set secrets.consumerSharedSecret="$FELIX_CONSUMER_SHARED_SECRET" \
  --set secrets.jwksPublic="$FELIX_JWKS_PUBLIC" \
  --set secrets.jwksPrivate="$FELIX_JWKS_PRIVATE"
```

The chart runs a **pre-install/pre-upgrade migrate Job**, then **api**, **worker**,
and **scheduler**. Disable with `--set migrate.enabled=false`. For lean `fs`
object store, set `persistence.enabled=true`.

See `deploy/helm/felix/values.yaml` for all knobs. Auth should be `jwt` or
`api_key` in production.

For manifest `secret:NAME` refs, compile pins, and the bundled `governed`
example, see [GOVERNANCE.md](../GOVERNANCE.md).
