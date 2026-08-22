# Felix Helm chart

Install from the repo root:

```bash
helm lint deploy/helm/felix
helm upgrade --install felix deploy/helm/felix -n felix --create-namespace
```

## Secrets

Pick **one** of:

| Mode | Values |
|------|--------|
| Existing Secret | `secrets.existingSecret: my-felix-secrets` (keys listed below) |
| External Secrets Operator | `externalSecrets.enabled: true` + a `SecretStore` / `ClusterSecretStore` |
| Chart bootstrap | leave both unset and fill `secrets.*` (dev only — empty stringData in git) |

Required Secret keys (env names):

- `FELIX_DATABASE_URL`
- `FELIX_REDIS_URL`
- `FELIX_ANTHROPIC_API_KEY` / `FELIX_OPENAI_API_KEY` (as needed)
- `FELIX_CONSUMER_SHARED_SECRET` (production / non-`none` auth)
- optional JWKS / S3 keys

### External Secrets example

```yaml
externalSecrets:
  enabled: true
  secretStore:
    name: aws-secretsmanager
    kind: ClusterSecretStore
  data:
    - secretKey: FELIX_DATABASE_URL
      remoteKey: prod/felix/database-url
    - secretKey: FELIX_ANTHROPIC_API_KEY
      remoteKey: prod/felix/anthropic-api-key

# Point Deployments at the synced Secret (defaults to release name):
secrets:
  existingSecret: ""  # uses chart fullname / ESO target
```

Manifest outbound refs (`secret:NAME`) resolve through `FELIX_SECRETS_BACKEND`
(env / AWS SM / GCP SM), not through this Helm Secret — keep names aligned.

See [../GOVERNANCE.md](../GOVERNANCE.md).
