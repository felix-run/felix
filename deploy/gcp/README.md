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

The chart runs **api**, **worker**, and **scheduler**. Apply migrations with
`felix migrate head` (Job not bundled yet). For lean `fs` object store, set
`persistence.enabled=true`.

See `deploy/helm/felix/values.yaml` for all knobs. Auth should be `jwt` or
`api_key` in production.
