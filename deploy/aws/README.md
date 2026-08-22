# Deploy Felix on AWS

Felix is cloud-agnostic. On AWS, map Protocols to managed services:

| Protocol | AWS |
|----------|-----|
| Postgres + pgvector | RDS Postgres (or Aurora) with `pgvector` |
| Cache | ElastiCache (Valkey/Redis) |
| Object store | S3 — set `FELIX_OBJECT_STORE=s3`, leave `FELIX_S3_ENDPOINT` empty for AWS |
| Secrets | `FELIX_SECRETS_BACKEND=aws` (Secrets Manager) with `felix-harness[aws]` |
| Compute | EKS (Helm chart in `deploy/helm/felix`) or ECS/Fargate |

## Minimal env

```bash
FELIX_CLOUD_PROVIDER=aws
FELIX_OBJECT_STORE=s3
FELIX_S3_ENDPOINT=          # empty → AWS default
FELIX_S3_BUCKET=your-bucket
FELIX_S3_REGION=us-east-1
FELIX_SECRETS_BACKEND=aws
FELIX_AWS_REGION=us-east-1
FELIX_DATABASE_URL=postgresql+psycopg://...
FELIX_REDIS_URL=redis://...
```

Use IRSA / instance roles instead of long-lived access keys when possible.
Install extras: `uv sync --extra aws` (or image build with `[aws]`).

## Helm

```bash
helm upgrade --install felix ./deploy/helm/felix \
  -f deploy/aws/values-eks.example.yaml
```

See `deploy/helm/felix/values.yaml` for all knobs. Auth should be `jwt` or `api_key` in production (`FELIX_ALLOW_INSECURE` must stay false).
