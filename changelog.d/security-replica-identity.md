**Every worker claimed fibers under the same name, so the lease-ownership guards decided
nothing.** `durability/fibers.py` asks whether a claim is its own with
`lease_owner == replica_id` — `_renew_lease` does it today, and it is what
`_release_fiber` and `_record_attempt` rely on. `FELIX_REPLICA_ID` defaulted to the constant
`"local"` and nothing ever set it: not the Helm chart, not a Compose overlay, and
`validate_runtime()` did not ask for it under `scale_out`. The only mention of it in the tree
was a commented-out line in `.env.example`.

So with `worker.replicaCount: 2` every pod claimed as `"local"`, `WHERE lease_owner = 'local'`
matched every claim including other pods', and `_renew_lease`'s own comment — *"a lease we
already lost must not be stolen back mid-step"* — described a guard that did not hold between
two replicas. It sat badly against the chart's own header on `deployment-worker.yaml`: *"Safe
to scale: every task is lease- or lock-protected."*

Two changes, because the default and the deployment are separate failures:

* `replica_id` now defaults to `{hostname}:{pid}` — stable within a process, distinct across
  them, which is exactly the property the predicates need. Host and pid rather than a uuid
  because this is a value an operator reads: in Kubernetes the hostname is the pod name, so a
  lease row names the pod holding it. Docker Compose already gives each container a distinct
  hostname, so the `compose.replicas.yml` stack is fixed by the default alone.
* The Helm chart sets it explicitly from the downward API (`metadata.name`), in the env tier
  every Felix deployment shares, so the identity does not depend on the container's hostname
  being meaningful and a new deployment template inherits it.

An empty `FELIX_REPLICA_ID` is now refused rather than silently defaulted. It would be worse
than the constant it replaces: `lease_owner` is `""` on every *unclaimed* row, so an empty id
would match every released claim as this worker's own.

**Nothing was broken end to end before this** — claim exclusion rests on `lease_until` plus
`FOR UPDATE SKIP LOCKED`, which never depended on the identity. What was missing was the
second line of defence those three predicates are written to provide. Found by the security
review on felix#262.
