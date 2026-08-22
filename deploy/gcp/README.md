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

## GCE + Compose

On the VM (`/opt/felix`), set:

```bash
FELIX_SECRETS_BACKEND=gcp
FELIX_GCP_PROJECT=your-project
FELIX_DOCKER_EXTRAS=gcp
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
  --project-directory . up -d --build
```

See `deploy/docker/README.md`.

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
