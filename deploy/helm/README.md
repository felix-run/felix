# Felix Helm chart

Install from the repo root:

```bash
helm lint deploy/helm/felix
helm upgrade --install felix deploy/helm/felix -n felix --create-namespace
```

## Topology

One Deployment per Felix process, so each can be sized and scaled for what it does:

| Deployment | Replicas | Scaled by | Probes |
|---|---|---|---|
| `<release>-api` | `api.replicaCount` | the HPA (`api.autoscaling.*`) | `/ready`, `/live` |
| `<release>-worker` | `worker.replicaCount` | you | liveness on `worker.metricsPort` |
| `<release>-scheduler` | always 1, `Recreate` | never | none |

The Service, the PDB and the HPA select the api alone. The scheduler is a singleton
because every instance enqueues every cron sweep on every tick — two schedulers double
every audit flush, retention pass and fiber resume. Workers are safe to scale: every task
is lease- or lock-protected.

Credentials are tiered by what each process reads. The worker gets the model and
object-store keys because it runs the agent loop (fiber resume, continuous eval), and
not the JWT signing key or the `/internal` shared secret — it executes tools on model
output, and nothing on its path reads those. The scheduler gets only the datastore URLs.

The worker's metrics port is its liveness signal and is **unauthenticated**, bound on all
interfaces, with tenant-supplied manifest ids in its labels. It is a containerPort, never
behind the Service, but any pod in the cluster can still reach it by pod IP. Restrict it
to your Prometheus with a NetworkPolicy, for example:

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: felix-worker-metrics
spec:
  podSelector:
    matchLabels:
      app.kubernetes.io/name: felix
      app.kubernetes.io/component: worker
  policyTypes: [Ingress]
  ingress:
    - from:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: monitoring
      ports:
        - port: 9464
```

The PDB (`api.podDisruptionBudget`) defaults to `maxUnavailable: 1` rather than
`minAvailable: 1`: with one replica the latter denies every voluntary eviction and a node
drain never completes.

With `FELIX_OBJECT_STORE=fs` the `/data` claim is shared by the api and worker pods, which
now schedule independently — a ReadWriteOnce claim cannot follow both to different nodes.
Use ReadWriteMany, pin both to one node, or use `s3`/`gcs`.

### Connection arithmetic

Each Felix process holds its own Postgres pool, so the ceiling is

```
(api replicas + worker replicas + 1 scheduler) × (FELIX_DB_POOL_SIZE + FELIX_DB_MAX_OVERFLOW)
```

which on the defaults (`10 + 20`) is 90 connections for one of each and 360 at
`autoscaling.maxReplicas: 10` with one worker — against a stock Postgres
`max_connections` of 100. Lower the pool settings in `config`, raise `max_connections`
knowing each backend costs memory, or put a transaction-mode pooler in front (the Compose
stack's `compose.pgbouncer.yml` shows the settings that go with one, including
`FELIX_DB_PREPARED_STATEMENTS`).

### Upgrading from chart 0.2.2 or earlier

Chart 0.2.2 and earlier ran all three processes in one Deployment named `<release>`.
`helm upgrade` deletes that Deployment and creates the three above; the api is briefly
absent between the two, because a Deployment's selector is immutable and the object had
to be replaced rather than rolled. Schedule the upgrade like a restart.

Four values keys moved under `api.` at the same time, because they only ever applied to
the api once the processes were split: `replicaCount`, `resources`, `autoscaling`,
`podDisruptionBudget`. A values file still setting them at the top level **fails the
render** with a message naming the new key, rather than being silently ignored — the old
`resources` also sized the worker, which now has `worker.resources`.

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
