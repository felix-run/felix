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
FELIX_IMAGE_TAG=0.18.0     # the published `-gcp` image already carries the gcp extra
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
