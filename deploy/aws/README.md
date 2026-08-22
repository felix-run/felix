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
FELIX_AUTH_MODE=jwt
FELIX_CONSUMER_SHARED_SECRET=$(openssl rand -hex 32)  # required for /internal
```

Use IRSA / instance roles instead of long-lived access keys when possible.
Install extras: `uv sync --extra aws` (or image build with `FELIX_EXTRAS=aws`).

## Helm

```bash
helm upgrade --install felix ./deploy/helm/felix \
  -f deploy/aws/values-eks.example.yaml \
  --set secrets.databaseUrl="$FELIX_DATABASE_URL" \
  --set secrets.redisUrl="$FELIX_REDIS_URL" \
  --set secrets.consumerSharedSecret="$FELIX_CONSUMER_SHARED_SECRET" \
  --set secrets.jwksPublic="$FELIX_JWKS_PUBLIC" \
  --set secrets.jwksPrivate="$FELIX_JWKS_PRIVATE"
```

The chart runs a **pre-install/pre-upgrade migrate Job** (`felix migrate head`), then
**api**, **worker**, and **scheduler**. Disable with `--set migrate.enabled=false`.
For lean `fs` object store (not recommended on EKS), set `persistence.enabled=true`
so `/data` uses a PVC.

See `deploy/helm/felix/values.yaml` for all knobs. Auth should be `jwt` or
`api_key` in production (`FELIX_ALLOW_INSECURE` must stay false).

For manifest `secret:NAME` refs, compile pins, and the bundled `governed`
example, see [GOVERNANCE.md](../GOVERNANCE.md).
