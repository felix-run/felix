# Deploy Felix on GCP

Felix is cloud-agnostic. On GCP, map Protocols to managed services:

| Protocol | GCP |
|----------|-----|
| Postgres + pgvector | Cloud SQL Postgres with `pgvector`, or AlloyDB |
| Cache | Memorystore (Redis/Valkey) |
| Object store | GCS — set `FELIX_OBJECT_STORE=gcs`, `FELIX_GCS_BUCKET=…` |
| Secrets | `FELIX_SECRETS_BACKEND=gcp` (Secret Manager) with `felix-harness[gcp]` |
| Compute | GKE (Helm chart in `deploy/helm/felix`) or Cloud Run (API + worker) |

## Minimal env

```bash
FELIX_CLOUD_PROVIDER=gcp
FELIX_OBJECT_STORE=gcs
FELIX_GCS_BUCKET=your-bucket
FELIX_GCP_PROJECT=your-project
FELIX_SECRETS_BACKEND=gcp
FELIX_DATABASE_URL=postgresql+psycopg://...
FELIX_REDIS_URL=redis://...
```

Prefer Workload Identity over JSON key files.
Install extras: `uv sync --extra gcp` (or image build with `[gcp]`).

## Helm

```bash
helm upgrade --install felix ./deploy/helm/felix \
  -f deploy/gcp/values-gke.example.yaml
```

See `deploy/helm/felix/values.yaml` for all knobs. Auth should be `jwt` or `api_key` in production.
